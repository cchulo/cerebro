"""The MCP gateway: one stateless streamable-HTTP endpoint, identity and policy in front of every tool.

Request path:  Starlette IdentityMiddleware (identity.resolve -> policy.grants, or 401 + challenge)
               -> FastMCP (tools/list filtered by the token's scopes; each tool checks its TokenScope, then the
               Grants for the scope / bank / unit it touches) -> the engine adapters, fanned out per unit.

Tools (TokenScope each needs):
  whoami(), list_scopes()                                              any authenticated caller
  query_docs(query, mode?, scopes?, fallback)                          docs.read
  live_search(source, query, scopes?, limit), live_fetch(source, ref)  docs.read
  search_code(query, scopes?, branch?, regex, max_results)             code.read
  list_code_units(), code_tool(unit, tool, arguments?, branch?)        code.read
  recall(query, bank?, budget, max_tokens), reflect(query, bank?, ...) memory.read
  retain(content, bank?, context?, tags?)                              memory.write

Adapters are built once at startup (Gateway.from_config) through cerebro.core.registry; tests pass fakes to
Gateway(...) directly. Units are addressed through the AdapterContext's Locator: the provisioner adapter when
cerebro.adapters.provision.<target> exists, else a StaticLocator("http://{unit}:8080").
"""
import asyncio
import functools
import json
import logging
import os
import re
import time
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from cerebro.core import AdapterContext, Config, EnvSecrets, Grants, Principal, StaticLocator, TokenScope, code_units, registry
from cerebro.core.contracts import (AccessPolicy, CodeIntelligence, DocumentIndex, IdentityProvider, MemoryStore,
                                    QueryOptions, RequestInfo, SearchHit)
from cerebro.core.types import Forbidden, Unauthenticated, Unsupported
from cerebro.core.units import units_for_repos
from cerebro.adapters.identity._bearer import WELL_KNOWN
from .identity import build_identity
from .live import LiveRegistry

log = logging.getLogger("cerebro.gateway")

# Sent to every client at connect time (MCP `instructions`); Claude Code / Cursor place it in the model's context,
# so agents learn the routing and the recall-first / retain-last habit without any client-side rules file.
INSTRUCTIONS = """Organisation context server. Use it before guessing.
- query_docs: what our documentation, ADRs, runbooks and service catalog say ("how do we", "who owns", "policy").
- search_code: exact code, symbols, file paths across the repositories you may read.
- code_tool: callers/callees, blast radius, dead code - structural questions against one code unit. Call
  list_code_units first to pick the unit and see which tools it offers.
- recall: at the START of a task, look up prior context (what was tried, decisions, corrections).
- live_search / live_fetch: when query_docs has no answer, search or read the system of record directly (Confluence, ...)
  within the same scopes; the index may lag a change by minutes.
- retain: at the END of a task, store the outcome in one or two sentences: what changed, decisions made, anything
  the docs got wrong. Use the personal bank unless the whole team should know (team bank from list_scopes).
- whoami / list_scopes: what you are allowed to see (scopes, repositories, memory banks).
Never retain content from restricted documents into a team bank. Prefer citing sources returned by query_docs."""

READ_ONLY = ToolAnnotations(readOnlyHint=True)
BIND_ENV = "CEREBRO_GATEWAY_BIND"          # set by the provisioner on the gateway unit: listen on 0.0.0.0


# ----------------------------------------------------------------------------------------------- helpers
def _brief(v, n: int = 100) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= n else s[:n] + "..."


_STOP = set("a an the of to in on for and or is are was were be been do does did how what which who whom whose when where why "
            "can could should would will shall may might must i we you they it this that these those with without from by as at "
            "into about our your their its my me us them there here please tell explain describe find show".split())


def keywords(query: str, limit: int = 8) -> str:
    """Systems of record search by terms, not sentences: keep the distinctive words of the question."""
    words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9_\-./]*", query) if w.lower() not in _STOP and len(w) > 1]
    seen, out = set(), []
    for w in words:
        if w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return " ".join(out[:limit]) or query


def request_info(request: Request) -> RequestInfo:
    return RequestInfo.from_headers(request.headers, method=request.method, path=request.url.path,
                                    client_host=request.client.host if request.client else None)


def _error(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}".rstrip(": ")           # httpx.ReadTimeout stringifies to "" - keep the type


# ----------------------------------------------------------------------------------------------- identity middleware
class IdentityMiddleware:
    """Pure ASGI middleware: for requests to the MCP path, resolve the Principal and its Grants before FastMCP
    sees the request, and stash them on request.state. Unauthenticated -> 401 with the provider's challenge."""

    def __init__(self, app, gateway: "Gateway"):
        self.app, self.gateway = app, gateway

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].rstrip("/") != self.gateway.mcp_path:
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        try:
            principal = await self.gateway.identity.resolve(request_info(request))
        except Unauthenticated as e:
            log.info("401 %s %s: %s", request.method, request.url.path, e)
            response = JSONResponse({"error": "invalid_token", "error_description": str(e)}, status_code=401,
                                    headers=self.gateway.identity.challenge())
            return await response(scope, receive, send)
        state = scope.setdefault("state", {})
        state["principal"] = principal
        state["grants"] = self.gateway.policy.grants(principal)
        await self.app(scope, receive, send)


class CerebroMCP(FastMCP):
    """FastMCP that hides tools the caller's token cannot use."""

    def __init__(self, gateway: "Gateway", **kwargs):
        super().__init__(**kwargs)
        self.gateway = gateway

    async def list_tools(self):
        tools = await super().list_tools()
        grants = self.gateway.current_grants()
        if grants is None:
            return tools
        return [t for t in tools if self.gateway.tool_visible(t.name, grants)]


# ----------------------------------------------------------------------------------------------- the gateway
class Gateway:
    def __init__(self, config: Config, *, identity: IdentityProvider, policy: AccessPolicy,
                 docs: DocumentIndex | None = None, code: CodeIntelligence | None = None,
                 memory: MemoryStore | None = None, live: LiveRegistry | None = None, notes: list[str] | None = None):
        self.config = config
        self.identity, self.policy = identity, policy
        self.docs, self.code, self.memory = docs, code, memory
        self.live = live or LiveRegistry(config)
        self.notes = list(notes or [])
        self.mcp_path = config.gateway.path.rstrip("/") or "/"
        self.tool_scopes: dict[str, TokenScope | None] = {}
        self.mcp = CerebroMCP(self, name="cerebro", instructions=INSTRUCTIONS, host=self.host, port=config.gateway.port,
                              streamable_http_path=config.gateway.path, stateless_http=True, json_response=True,
                              transport_security=self._transport_security())
        register_tools(self)
        register_prompts(self.mcp)
        self.app = self._build_app()

    # ---- construction
    @classmethod
    def from_config(cls, config: Config) -> "Gateway":
        """Build every adapter through the registry. A missing engine adapter module is a warning, not a crash:
        the corresponding tools then fail with a clear message, and the rest keeps serving."""
        notes: list[str] = []
        secrets = EnvSecrets()
        try:
            locator = registry.build("provision", config.provisioning.target, config.provisioning.options,
                                     AdapterContext(config, secrets=secrets))
        except LookupError as e:
            notes.append(f"{e}; units resolve through StaticLocator(http://{{unit}}:8080)")
            log.warning("%s", notes[-1])
            locator = StaticLocator(template="http://{unit}:8080")
        ctx = AdapterContext(config, secrets=secrets, locator=locator)
        identity = build_identity(config, ctx)
        policy = registry.build("policy", config.policy.type, config.policy.options, ctx)

        def engine(kind: str, cfg):
            if cfg is None:
                return None
            try:
                return registry.build(kind, cfg.type, cfg.options, ctx)
            except LookupError as e:
                notes.append(str(e))
                log.warning("%s; %s tools will fail until the adapter exists", e, kind)
                return None

        gw = cls(config, identity=identity, policy=policy, docs=engine("docs", config.engines.docs),
                 code=engine("code", config.engines.code), memory=engine("memory", config.engines.memory), notes=notes)
        return gw

    @property
    def host(self) -> str:
        """identity.mode none binds to loopback unless allow_remote (then a static token guards every request)."""
        ident = self.config.identity
        if ident.mode == "none" and not ident.allow_remote:
            return "127.0.0.1"
        return self.config.gateway.host

    def bind_host(self) -> str:
        """Address uvicorn listens on. `gateway.host` is where clients reach the gateway (the address the provisioner
        publishes the port on); inside a workload the process itself must listen on every interface or the published
        port / Service never reaches it, which the provisioner signals with $CEREBRO_GATEWAY_BIND. Mode none without
        allow_remote stays on loopback whatever the environment says."""
        ident = self.config.identity
        if ident.mode == "none" and not ident.allow_remote:
            return "127.0.0.1"
        return os.environ.get(BIND_ENV) or self.config.gateway.host

    def _transport_security(self) -> TransportSecuritySettings | None:
        """FastMCP's DNS-rebinding protection (Host header allowlist) only fits the loopback case; behind a proxy
        or a public URL the Host header is the public name, so it is off there."""
        if self.host in ("127.0.0.1", "localhost", "::1") and not self.config.gateway.public_url:
            return None                                     # FastMCP default: loopback allowlist
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    def _build_app(self):
        mcp = self.mcp

        async def metadata(request: Request) -> Response:
            md = self.identity.protected_resource_metadata()
            if md is None:
                return JSONResponse({"error": "not_found", "error_description": "no OAuth protected resource metadata in this identity mode"}, status_code=404)
            return JSONResponse(md)

        async def health(request: Request) -> Response:
            return JSONResponse({"ok": True, "identity": self.identity.name, "policy": self.policy.name,
                                 "engines": {"docs": getattr(self.docs, "name", None), "code": getattr(self.code, "name", None),
                                             "memory": getattr(self.memory, "name", None)},
                                 "notes": self.notes})

        mcp.custom_route(WELL_KNOWN, methods=["GET"])(metadata)
        mcp.custom_route(WELL_KNOWN + "/{suffix:path}", methods=["GET"])(metadata)   # RFC 9728 path-suffixed form
        mcp.custom_route("/health", methods=["GET"])(health)
        app = mcp.streamable_http_app()
        app.add_middleware(IdentityMiddleware, gateway=self)
        return app

    # ---- per-request identity
    def current_grants(self) -> Grants | None:
        try:
            request = self.mcp._mcp_server.request_context.request
        except LookupError:
            return None
        return getattr(getattr(request, "state", None), "grants", None) if request is not None else None

    def grants_of(self, ctx: Context) -> Grants:
        request = ctx.request_context.request
        grants = getattr(getattr(request, "state", None), "grants", None) if request is not None else None
        if grants is None:
            raise Unauthenticated("no identity on this request")
        return grants

    def principal_of(self, ctx: Context) -> Principal:
        return ctx.request_context.request.state.principal

    def tool_visible(self, name: str, grants: Grants) -> bool:
        scope = self.tool_scopes.get(name)
        return scope is None or scope.value in grants.token_scopes or TokenScope.ADMIN.value in grants.token_scopes

    # ---- tool registration: token-scope gate + activity log around every tool
    def tool(self, scope: TokenScope | None, *, read_only: bool = True):
        def register(fn):
            name = fn.__name__
            self.tool_scopes[name] = scope

            def who(grants: Grants) -> str:
                extra = [b.removeprefix("team-") for b in grants.team_banks]
                return f"{grants.subject}[{','.join(extra) or '-'}]"

            def args(kw) -> str:
                return " ".join(f"{k}={_brief(v)}" for k, v in kw.items() if k != "ctx" and v is not None)

            @functools.wraps(fn)
            async def wrapper(*a, **kw):
                grants = self.grants_of(kw["ctx"])
                t = time.monotonic()
                log.info("call %s  by %s  %s", name, who(grants), args(kw))
                try:
                    if scope is not None:
                        grants.check_token_scope(scope)
                    r = await fn(*a, **kw)
                except Exception as e:
                    log.info("fail %s  %.1fs  %s: %s", name, time.monotonic() - t, type(e).__name__, _brief(str(e), 140))
                    raise
                log.info("done %s  %.1fs", name, time.monotonic() - t)
                return r

            self.mcp.add_tool(wrapper, name=name, description=fn.__doc__, annotations=READ_ONLY if read_only else None)
            return fn
        return register

    # ---- engines
    def need_docs(self) -> DocumentIndex:
        if self.docs is None:
            raise RuntimeError("no document engine: engines.docs is unset or its adapter is missing")
        return self.docs

    def need_code(self) -> CodeIntelligence:
        if self.code is None:
            raise RuntimeError("no code engine: engines.code is unset or its adapter is missing")
        return self.code

    def need_memory(self) -> MemoryStore:
        if self.memory is None:
            raise RuntimeError("no memory engine: engines.memory is unset or its adapter is missing")
        return self.memory

    async def bounded(self, coros: list) -> list:
        """Run per-unit calls with at most gateway.concurrency in flight; exceptions come back as values."""
        sem = asyncio.Semaphore(max(1, self.config.gateway.concurrency))

        async def one(c):
            async with sem:
                return await c
        return await asyncio.gather(*(one(c) for c in coros), return_exceptions=True)

    # ---- live fallback
    async def fallback_search(self, grants: Grants, query: str, scopes: list[str]) -> dict:
        """Live sources with fallback enabled, searched for the scopes whose index missed."""
        hits: dict[str, Any] = {}
        terms = keywords(query)
        for name in self.live.fallbacks():
            allowed = self.live.allowed(grants, name, scopes)
            if not allowed:
                continue
            try:
                res = await self.live.load(name).search(terms, allowed, limit=5)
            except Exception as e:                       # a dead upstream must not break query_docs
                hits[name] = {"error": _error(e)[:200]}
                continue
            if res:
                hits[name] = {"query": terms, "results": res,
                              "note": f"index had no answer for {scopes}; use live_fetch(\"{name}\", ref) to read one"}
        return hits


# ----------------------------------------------------------------------------------------------- tools
def register_tools(gw: Gateway) -> None:
    config = gw.config

    @gw.tool(None)
    async def whoami(ctx: Context) -> dict:
        """Who the gateway thinks you are and what you may touch: subject, kind, groups, token scopes, scopes,
        repositories (with tracked branches), memory banks."""
        p, g = gw.principal_of(ctx), gw.grants_of(ctx)
        return {"subject": p.subject, "kind": p.kind, "display_name": p.display_name, "issuer": p.issuer,
                "groups": sorted(p.groups), "token_scopes": sorted(g.token_scopes), "scopes": g.scopes,
                "repos": [{"name": r.name, "url": r.url, "scope": r.scope, "branches": r.branches} for r in g.repos],
                "banks": g.banks, "personal_bank": g.personal_bank, "team_banks": g.team_banks}

    @gw.tool(None)
    async def list_scopes(ctx: Context) -> dict:
        """List the documentation/code scopes, repositories and memory banks the current user can access."""
        g = gw.grants_of(ctx)
        return {"user": g.subject, "scopes": g.scopes, "repos": [r.url for r in g.repos], "banks": g.banks}

    # ---- docs
    @gw.tool(TokenScope.DOCS_READ)
    async def query_docs(ctx: Context, query: str, mode: str | None = None, scopes: list[str] | None = None,
                         fallback: bool = True) -> dict:
        """Ask the documentation indexes (Confluence, Backstage, repo docs, ADRs, requirements).
        mode: engine-specific (LightRAG: local | global | hybrid | mix | naive); omit for the engine's default.
        Only scopes you may read are queried; omit `scopes` to query all of them. If a scope's index has no answer
        and fallback is true, the enabled live sources (systems of record) are searched for that scope and their
        hits are returned under `fallback` for live_fetch."""
        g = gw.grants_of(ctx)
        docs = gw.need_docs()
        targets = g.check_scopes(scopes)
        if not targets:
            return {"results": [], "note": "user has no document scopes"}
        m = docs.check_mode(mode)

        async def one(scope: str) -> dict:
            a = await docs.query(scope, query, QueryOptions(mode=m))
            return {"scope": scope, "answer": a.answer, "answered": a.answered,
                    "references": [r.model_dump(exclude_none=True) for r in a.references]}

        results = await gw.bounded([one(s) for s in targets])
        results = [r if not isinstance(r, BaseException) else {"scope": s, "error": _error(r), "answered": False}
                   for s, r in zip(targets, results)]
        out: dict[str, Any] = {"mode": m, "results": results}
        missed = [r["scope"] for r in results if not r.get("answered")]
        if fallback and missed:
            out["fallback"] = await gw.fallback_search(g, query, missed)
        return out

    @gw.tool(TokenScope.DOCS_READ)
    async def live_search(ctx: Context, source: str, query: str, scopes: list[str] | None = None, limit: int = 10) -> dict:
        """Search a system of record directly (e.g. source="confluence") when query_docs missed or may be stale.
        Confined to the spaces/projects of your scopes; restricted pages are never returned. Returns refs for live_fetch."""
        g = gw.grants_of(ctx)
        allowed = gw.live.allowed(g, source, scopes)
        if not allowed:
            return {"source": source, "results": [], "note": f"none of your scopes lists '{source}'"}
        return {"source": source, "allowed": allowed, "results": await gw.live.load(source).search(query, allowed, limit)}

    @gw.tool(TokenScope.DOCS_READ)
    async def live_fetch(ctx: Context, source: str, ref: str, max_chars: int = 20000) -> dict:
        """Read one item from a system of record by the ref live_search returned (e.g. a Confluence page id).
        Refused if the item is outside your scopes or carries its own read restriction."""
        g = gw.grants_of(ctx)
        allowed = gw.live.allowed(g, source)
        if not allowed:
            raise Forbidden(f"none of your scopes lists '{source}'")
        return await gw.live.load(source).fetch(ref, allowed, max_chars)

    # ---- code
    @gw.tool(TokenScope.CODE_READ)
    async def search_code(ctx: Context, query: str, scopes: list[str] | None = None, branch: str | None = None,
                          regex: bool = False, max_results: int = 20) -> dict:
        """Search source code across the repositories you can read. Fans out to every code unit that serves one of
        your repositories and merges the hits. `branch` selects a tracked branch on engines that keep per-branch
        indexes (an error on engines that do not); regex=true treats the query as a regular expression."""
        g = gw.grants_of(ctx)
        code = gw.need_code()
        repos = g.repos_in(g.check_scopes(scopes))
        allowed = {r.name for r in repos}
        groups = units_for_repos(config, repos)
        if not groups:
            return {"query": query, "hits": [], "units": [], "note": "no repositories in your scopes"}
        results = await gw.bounded([
            code.search(unit, query, repos=[r.name for r in mine], branch=branch, regex=regex, max_results=max_results)
            for unit, mine in groups])
        hits: list[dict] = []
        errors: dict[str, str] = {}
        for (unit, _), res in zip(groups, results):
            if isinstance(res, Unsupported):
                raise Unsupported(f"{unit.name}: {res}")
            if isinstance(res, BaseException):
                errors[unit.name] = _error(res)
                continue
            for h in res:                                   # belt and braces: nothing outside the caller's repos leaves
                if isinstance(h, SearchHit) and h.repository.lower() in allowed:
                    hits.append({**h.model_dump(exclude_none=True), "unit": unit.name})
        out = {"query": query, "units": [u.name for u, _ in groups], "hits": hits[:max_results]}
        if branch:
            out["branch"] = branch
        if errors:
            out["errors"] = errors
        return out

    @gw.tool(TokenScope.CODE_READ)
    async def list_code_units(ctx: Context) -> dict:
        """The code units (one per scope or per repository, see engines.code.unit) you can query with code_tool,
        each with its engine capabilities and the read-only tools it offers."""
        g = gw.grants_of(ctx)
        code = gw.need_code()
        units = code_units(config, g.scopes)
        caps = await gw.bounded([code.capabilities(u) for u in units])
        out = []
        for u, c in zip(units, caps):
            entry = {"name": u.name, "scope": u.scope, "kind": u.kind,
                     "repos": [{"name": r.name, "branches": r.branches} for r in u.repos]}
            entry["capabilities" if not isinstance(c, BaseException) else "error"] = \
                c.model_dump(exclude_none=True) if not isinstance(c, BaseException) else _error(c)
            out.append(entry)
        return {"units": out}

    @gw.tool(TokenScope.CODE_READ)
    async def code_tool(ctx: Context, unit: str, tool: str, arguments: dict | None = None, branch: str | None = None) -> dict:
        """Call one read-only code-intelligence tool (callers/callees, dead code, complexity, graph queries) on ONE
        code unit you can read. list_code_units shows the units and their tools."""
        g = gw.grants_of(ctx)
        code = gw.need_code()
        units = {u.name: u for u in code_units(config, g.scopes)}
        if unit not in units:
            raise Forbidden(f"{g.subject} is not allowed to access code unit '{unit}'; allowed: {sorted(units)}")
        res = await code.call(units[unit], tool, arguments or {}, branch=branch)
        return res.model_dump(exclude_none=True)

    # ---- memory
    @gw.tool(TokenScope.MEMORY_READ)
    async def recall(ctx: Context, query: str, bank: str | None = None, budget: str = "mid", max_tokens: int = 4096) -> dict:
        """Recall relevant memories (prior work, decisions, corrections). Defaults to your personal bank;
        pass a team bank (see list_scopes) for shared memory. budget: low | mid | high."""
        g = gw.grants_of(ctx)
        mem = gw.need_memory()
        b = g.check_bank(bank)
        res = await mem.recall(b, query, budget=mem.check_budget(budget), max_tokens=max_tokens)
        return res.model_dump(exclude_none=True, exclude={"raw"})

    @gw.tool(TokenScope.MEMORY_WRITE, read_only=False)
    async def retain(ctx: Context, content: str, bank: str | None = None, context: str | None = None,
                     tags: list[str] | None = None) -> dict:
        """Store what happened: outcomes, decisions, things the docs got wrong. Never store restricted-doc content
        in a team bank the whole team can read."""
        g = gw.grants_of(ctx)
        mem = gw.need_memory()
        b = g.check_bank(bank)
        res = await mem.retain(b, content, context=context, tags=tags)
        return res.model_dump(exclude_none=True, exclude={"raw"})

    if gw.memory is None or gw.memory.supports_reflect:
        @gw.tool(TokenScope.MEMORY_READ)
        async def reflect(ctx: Context, query: str, bank: str | None = None, budget: str = "low",
                          context: str | None = None) -> dict:
            """Ask the memory bank to reason over everything it knows about a question. budget: low | mid | high."""
            g = gw.grants_of(ctx)
            mem = gw.need_memory()
            b = g.check_bank(bank)
            res = await mem.reflect(b, query, budget=mem.check_budget(budget), context=context)
            return res.model_dump(exclude_none=True, exclude={"raw"})


# ----------------------------------------------------------------------------------------------- prompts
# MCP prompts show up as slash commands in Claude Code (/mcp__cerebro__start_task, /mcp__cerebro__wrap_up) and as
# prompt pickers in other clients: a deterministic way to trigger recall / retain, and hookable (Claude Code Stop hook).
def register_prompts(mcp: FastMCP) -> None:
    @mcp.prompt()
    def start_task(task: str) -> str:
        """Begin a task: recall prior context, then plan using docs and code before changing anything."""
        return (f"I am starting this task: {task}\n\n"
                "1. Call recall with a short query describing the task (and the team bank if list_scopes shows one).\n"
                "2. Call query_docs for the relevant runbooks/ADRs/policies and search_code or code_tool for the code.\n"
                "3. Summarise what you learned and any prior decisions or corrections before proposing changes.")

    @mcp.prompt()
    def wrap_up() -> str:
        """Finish a task: retain the outcome, decisions and anything the docs got wrong."""
        return ("The task is finished. Call retain once with one or two sentences covering: what changed, decisions made "
                "and why, anything the documentation got wrong or was missing. Pick the team bank only if the whole team "
                "should know; never include content from restricted documents. Then confirm what was stored.")

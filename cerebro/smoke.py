"""cerebro smoke: the access-control smoke test (v1 scripts/smoke-test.py) against a live gateway, over MCP with
static bearer tokens.

    cerebro smoke [-c cerebro.yaml] [--env-file secrets.env] [--url http://127.0.0.1:8090/mcp]
                  [--as alice ...] [--token T] [--probes tests/e2e/probes.yaml] [--live] [--only identity,docs,...]

Personas are the entries of identity.tokens (mode static): subject, token, groups. What each MUST and MUST NOT see
is computed in-process with the configured policy adapter (the same code the gateway runs), so no second table of
expectations exists. Per persona:

    identity   whoami / list_scopes match the policy (scopes, repos, banks, token scopes); tools/list hides tools the
               token lacks; a tool outside the token scopes is refused
    docs       query_docs on a scope outside the grants is refused; a scope in the grants answers or misses cleanly
    code       search_code returns only repositories of the caller's scopes; list_code_units only the caller's
               units; code_tool on a foreign unit is refused; `branch` works on a tracked branch and is refused for
               an unknown one (units whose engine declares branches)
    memory     retain + recall on the personal bank and on every team bank; a bank of another persona is refused
    probes     --probes FILE: queries whose answer must carry a marker for some personas and never for the others
               (content isolation proven without depending on LLM wording); tests/e2e/probes.yaml matches tests/fixtures
    live       --live: live_search confined to the caller's spaces, live_fetch refused outside them and on
               restricted pages, query_docs fallback consults the live source (from the probes file's `live:`)

Usable against any deployment: `--url` points anywhere, `--as` limits to some personas, `--token` overrides the token
of the one persona named with `--as`. Exit status 1 when a check failed. Nothing here writes anywhere but the memory
banks (one retain per bank, tagged `smoke`).
"""
from __future__ import annotations
import argparse, asyncio, contextlib, datetime, json, logging, os, sys, time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable
import yaml
from cerebro.core import Config, Grants, TokenScope, code_units, load_config, registry
from cerebro.core.config import read_env_file
from cerebro.core.context import AdapterContext
from cerebro.adapters.identity._claims import principal_from_seed

log = logging.getLogger("cerebro.smoke")
PARTS = ("identity", "docs", "code", "memory", "probes", "live")
TOOL_SCOPES = {"whoami": None, "list_scopes": None, "query_docs": TokenScope.DOCS_READ, "live_search": TokenScope.DOCS_READ,
               "live_fetch": TokenScope.DOCS_READ, "search_code": TokenScope.CODE_READ, "list_code_units": TokenScope.CODE_READ,
               "code_tool": TokenScope.CODE_READ, "recall": TokenScope.MEMORY_READ, "reflect": TokenScope.MEMORY_READ,
               "retain": TokenScope.MEMORY_WRITE}


# ----------------------------------------------------------------------------------------------- personas
@dataclass
class Persona:
    name: str
    token: str
    groups: list[str]
    kind: str
    grants: Grants

    def has(self, scope: TokenScope) -> bool:
        return scope.value in self.grants.token_scopes or TokenScope.ADMIN.value in self.grants.token_scopes

    @property
    def repo_names(self) -> set[str]:
        return {r.name for r in self.grants.repos}


def personas_from_config(config: Config) -> dict[str, Persona]:
    """subject -> Persona with the Grants the gateway's policy computes for it."""
    if config.identity.mode != "static":
        raise SystemExit(f"cerebro smoke needs identity.mode static (tokens per persona); config says {config.identity.mode}")
    policy = registry.build("policy", config.policy.type, config.policy.options, AdapterContext(config))
    out: dict[str, Persona] = {}
    for token, seed in config.identity.tokens.items():
        principal = principal_from_seed(seed, issuer="static")
        out[seed.subject] = Persona(seed.subject, token, sorted(seed.groups), seed.kind, policy.grants(principal))
    return out


def load_probes(path: str | os.PathLike | None) -> dict:
    if not path:
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


# ----------------------------------------------------------------------------------------------- transport
Connect = Callable[[str], contextlib.AbstractAsyncContextManager]


def http_connect(url: str, timeout: float) -> Connect:
    """token -> an initialised ClientSession over streamable HTTP with a bearer header."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    import httpx

    @contextlib.asynccontextmanager
    async def connect(token: str) -> AsyncIterator[ClientSession]:
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=httpx.Timeout(timeout, connect=10)) as client:
            async with streamable_http_client(url, http_client=client) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    yield s
    return connect


# ----------------------------------------------------------------------------------------------- the test
@dataclass
class Result:
    part: str
    persona: str
    check: str
    ok: bool
    detail: str = ""


@dataclass
class Smoke:
    config: Config
    personas: dict[str, Persona]
    connect: Connect
    probes: dict = field(default_factory=dict)
    live: bool = False
    only: tuple[str, ...] = PARTS
    verbose: bool = False
    out: Callable[[str], None] = print
    results: list[Result] = field(default_factory=list)
    timings: list[tuple[str, str, float]] = field(default_factory=list)
    _part: str = "identity"

    # ---- plumbing
    def check(self, persona: str, name: str, cond: bool, detail: str = "") -> bool:
        self.results.append(Result(self._part, persona, name, bool(cond), detail))
        self.out(f"  {'ok  ' if cond else 'FAIL'} [{persona}] {name}" + (f": {detail}" if detail and (self.verbose or not cond) else ""))
        return bool(cond)

    async def call(self, p: Persona, tool_name: str, **args) -> tuple[bool, Any, str]:
        """(is_error, structured or parsed result, text)."""
        t0 = time.monotonic()
        async with self.connect(p.token) as s:
            res = await s.call_tool(tool_name, args)
        dt = time.monotonic() - t0
        self.timings.append((p.name, tool_name, dt))
        text = "".join(getattr(b, "text", "") for b in res.content)
        if self.verbose:
            shown = {k: (v if len(json.dumps(v)) < 60 else json.dumps(v)[:57] + "...") for k, v in args.items()}
            self.out(f"    > {p.name} {tool_name} {json.dumps(shown)} -> {'error' if res.isError else 'ok'} {dt:.1f}s")
        if res.isError:
            return True, None, text
        data = res.structuredContent
        if data is None:
            try:
                data = json.loads(text)
            except ValueError:
                data = text
        return False, data, text

    async def tool_names(self, p: Persona) -> set[str]:
        async with self.connect(p.token) as s:
            return {t.name for t in (await s.list_tools()).tools}

    def _foreign_scope(self, p: Persona) -> str | None:
        return next((s for s in self.config.scopes if s not in p.grants.scopes), None)

    def _foreign_bank(self, p: Persona) -> str | None:
        for q in self.personas.values():
            for b in q.grants.banks:
                if b not in p.grants.banks:
                    return b
        return None

    # ---- parts
    async def identity(self, p: Persona) -> None:
        self._part = "identity"
        g = p.grants
        err, me, text = await self.call(p, "whoami")
        if not self.check(p.name, "whoami answers", not err, text[:160]):
            return
        self.check(p.name, "whoami subject/kind", me.get("subject") == p.name and me.get("kind") == p.kind, f"{me.get('subject')}/{me.get('kind')}")
        self.check(p.name, "whoami scopes match the policy", me.get("scopes") == g.scopes, f"{me.get('scopes')} vs {g.scopes}")
        self.check(p.name, "whoami banks match the policy", me.get("banks") == g.banks and me.get("personal_bank") == g.personal_bank,
                   f"{me.get('banks')} vs {g.banks}")
        self.check(p.name, "whoami repos match the policy", {r["name"] for r in me.get("repos", [])} == p.repo_names,
                   f"{sorted(r['name'] for r in me.get('repos', []))}")
        self.check(p.name, "whoami token scopes", set(me.get("token_scopes", [])) == set(g.token_scopes), f"{me.get('token_scopes')}")
        err, ls, text = await self.call(p, "list_scopes")
        self.check(p.name, "list_scopes shape", not err and ls.get("user") == p.name and ls.get("scopes") == g.scopes
                   and ls.get("banks") == g.banks and set(ls.get("repos", [])) == {r.url for r in g.repos}, text[:160])
        names = await self.tool_names(p)
        expected_hidden = {t for t, s in TOOL_SCOPES.items() if s is not None and not p.has(s)}
        self.check(p.name, "tools/list hides tools the token lacks", not (expected_hidden & names), f"visible: {sorted(names)}")
        for tool in sorted(expected_hidden, key=lambda t: (t != "query_docs", t)):   # query_docs first when hidden
            args = {"query": "x"} if tool in ("query_docs", "recall", "reflect", "search_code") else \
                   {"content": "x"} if tool == "retain" else {"source": "confluence", "query": "x"} if tool == "live_search" else \
                   {"source": "confluence", "ref": "1"} if tool == "live_fetch" else {"unit": "x", "tool": "x"} if tool == "code_tool" else {}
            err, _, text = await self.call(p, tool, **args)
            self.check(p.name, f"{tool} refused without its token scope", err and "token lacks scope" in text, text[:120])
            break                                                        # one is proof enough

    async def docs(self, p: Persona) -> None:
        self._part = "docs"
        if not p.has(TokenScope.DOCS_READ):
            return
        foreign = self._foreign_scope(p)
        if foreign:
            err, _, text = await self.call(p, "query_docs", query="x", scopes=[foreign], fallback=False)
            self.check(p.name, f"query_docs scope={foreign} refused", err and "not allowed" in text, text[:120])
        if p.grants.scopes:
            err, out, text = await self.call(p, "query_docs", query="What is documented here?", mode="naive", fallback=False)
            ok = not err and {r["scope"] for r in out.get("results", [])} == set(p.grants.scopes)
            self.check(p.name, "query_docs fans out to exactly the caller's scopes", ok, text[:160])
            if not err:
                bad = [r["scope"] for r in out["results"] if r.get("error")]
                self.check(p.name, "every scope's index answered (no engine errors)", not bad,
                           "; ".join(f"{r['scope']}: {r.get('error')}" for r in out["results"] if r.get("error"))[:200])

    async def code(self, p: Persona) -> None:
        self._part = "code"
        if not p.has(TokenScope.CODE_READ):
            return
        mine = code_units(self.config, p.grants.scopes)
        err, out, text = await self.call(p, "list_code_units")
        if not self.check(p.name, "list_code_units answers", not err, text[:160]):
            return
        units = {u["name"]: u for u in out.get("units", [])}
        self.check(p.name, "list_code_units shows exactly the caller's units", set(units) == {u.name for u in mine}, f"{sorted(units)}")
        for name, u in units.items():
            self.check(p.name, f"unit {name} reports capabilities", "capabilities" in u, u.get("error", "")[:160])
        err, out, text = await self.call(p, "search_code", query="import", max_results=20)
        if self.check(p.name, "search_code answers", not err, text[:160]):
            repos = {h["repository"] for h in out.get("hits", [])}
            self.check(p.name, "search_code hits only repositories of the caller's scopes", repos <= p.repo_names,
                       f"{sorted(repos)}" + (f" errors={out['errors']}" if out.get("errors") else ""))
            self.check(p.name, "search_code found something", bool(out.get("hits")), f"units={out.get('units')} errors={out.get('errors')}")
        foreign = next((u for u in code_units(self.config) if u.name not in units), None)
        if foreign:
            err, _, text = await self.call(p, "code_tool", unit=foreign.name, tool="unit_info")
            self.check(p.name, f"code_tool on foreign unit {foreign.name} refused", err and "not allowed" in text, text[:120])
        foreign_scope = self._foreign_scope(p)
        if foreign_scope and self.config.scopes[foreign_scope].code.repos:
            err, _, text = await self.call(p, "search_code", query="x", scopes=[foreign_scope])
            self.check(p.name, f"search_code scope={foreign_scope} refused", err and "not allowed" in text, text[:120])
        # branches: a unit whose engine declares them and a repo with a tracked non-default branch
        for name, u in units.items():
            caps = u.get("capabilities") or {}
            tracked = [(r["name"], b) for r in u.get("repos", []) for b in r.get("branches", [])]
            if not caps.get("branches") or not tracked:
                continue
            repo, branch = tracked[-1]
            err, out, text = await self.call(p, "search_code", query="import", scopes=[u["scope"]], branch=branch, max_results=5)
            self.check(p.name, f"search_code branch={branch} on {repo} works", not err and any(
                h.get("repository") == repo for h in out.get("hits", [])) or (not err and out.get("branch") == branch and not out.get("errors")),
                text[:200])
            err, out, text = await self.call(p, "search_code", query="import", scopes=[u["scope"]], branch="no-such-branch-x")
            refused = err or (not out.get("hits") and bool(out.get("errors")))
            self.check(p.name, "search_code with an unknown branch is refused", refused, text[:200])
            tool = next((t["name"] for t in caps.get("tools", []) if t["name"] not in ("grep", "unit_info")
                         and not (t.get("input_schema") or {}).get("required")), None)
            if tool:
                err, out, text = await self.call(p, "code_tool", unit=name, tool=tool, arguments={"repo": repo}, branch=branch)
                self.check(p.name, f"code_tool {tool} branch={branch} works", not err and not out.get("is_error"), text[:200])
                err, out, text = await self.call(p, "code_tool", unit=name, tool=tool, arguments={"repo": repo}, branch="no-such-branch-x")
                self.check(p.name, f"code_tool {tool} with an unknown branch is refused", err or out.get("is_error"), text[:200])
            break

    async def memory(self, p: Persona) -> None:
        self._part = "memory"
        g = p.grants
        if p.has(TokenScope.MEMORY_WRITE):
            for bank in g.banks:
                err, out, text = await self.call(p, "retain", content=f"smoke: {p.name} ran the smoke test on {datetime.date.today()} (bank {bank})",
                                                 bank=bank, tags=["smoke"])
                self.check(p.name, f"retain to {bank}", not err and out.get("accepted", True), text[:160])
        if p.has(TokenScope.MEMORY_READ):
            for bank in g.banks:
                err, out, text = await self.call(p, "recall", query="smoke test", bank=bank, budget="low")
                self.check(p.name, f"recall from {bank}", not err and out.get("bank") == bank, text[:160])
            if g.personal_bank is None:
                err, _, text = await self.call(p, "recall", query="x")
                self.check(p.name, "service principal must name a team bank", err and "no personal bank" in text, text[:120])
        foreign = self._foreign_bank(p)
        if foreign and p.has(TokenScope.MEMORY_READ):
            err, _, text = await self.call(p, "recall", query="x", bank=foreign)
            self.check(p.name, f"recall from foreign bank {foreign} refused", err and "not allowed" in text, text[:120])
        if foreign and p.has(TokenScope.MEMORY_WRITE):
            err, _, text = await self.call(p, "retain", content="x", bank=foreign)
            self.check(p.name, f"retain to foreign bank {foreign} refused", err and "not allowed" in text, text[:120])

    async def probe_docs(self, probe: dict) -> None:
        self._part = "probes"
        marker, scope, query = probe["marker"], probe.get("scope"), probe["query"]
        label = probe.get("name", marker)
        for name in probe.get("visible_to", []):
            p = self.personas.get(name)
            if p is None or not p.has(TokenScope.DOCS_READ):
                continue
            args = {"query": query, "mode": probe.get("mode", "mix")}
            if scope and scope in p.grants.scopes:
                args["scopes"] = [scope]
            err, out, text = await self.call(p, "query_docs", **args)
            self.check(p.name, f"{label}: {marker} visible", not err and marker in text,
                       (text[:200] if err else "; ".join(f"{r['scope']}: {(r.get('answer') or r.get('error') or '')[:90]}" for r in out.get("results", []))))
        for name in probe.get("hidden_from", []):
            p = self.personas.get(name)
            if p is None or not p.has(TokenScope.DOCS_READ):
                continue
            err, out, text = await self.call(p, "query_docs", query=query, mode=probe.get("mode", "mix"))
            self.check(p.name, f"{label}: {marker} never returned", not err and marker not in text, text[:200])

    async def live_checks(self, live: dict) -> None:
        self._part = "live"
        source = live.get("source", "confluence")
        for s in live.get("search", []):
            for name in s.get("visible_to", []):
                p = self.personas[name]
                err, out, text = await self.call(p, "live_search", source=source, query=s["query"])
                refs = [r.get("ref") for r in out.get("results", [])] if not err else []
                self.check(p.name, f"live_search '{s['query']}' finds {s['ref']}", not err and s["ref"] in refs, text[:200])
                if not err and s.get("space"):
                    self.check(p.name, "live_search results stay within the caller's spaces",
                               all(r.get("space") in {sp for a in out.get("allowed", []) for sp in a.get("spaces", [])} for r in out["results"]),
                               f"{[r.get('space') for r in out['results']]}")
            for name in s.get("hidden_from", []):
                p = self.personas[name]
                err, out, text = await self.call(p, "live_search", source=source, query=s["query"])
                refs = [r.get("ref") for r in out.get("results", [])] if not err else []
                self.check(p.name, f"live_search '{s['query']}' never returns {s['ref']}", not err and s["ref"] not in refs, text[:200])
        for f in live.get("fetch", []):
            for name in f.get("visible_to", []):
                p = self.personas[name]
                err, out, text = await self.call(p, "live_fetch", source=source, ref=f["ref"])
                self.check(p.name, f"live_fetch {f['ref']} reads {f.get('marker', '')}", not err and f.get("marker", "") in text, text[:200])
            for name in f.get("hidden_from", []):
                p = self.personas[name]
                err, _, text = await self.call(p, "live_fetch", source=source, ref=f["ref"])
                self.check(p.name, f"live_fetch {f['ref']} refused", err and ("outside your scopes" in text or "restriction" in text), text[:200])
        for fb in live.get("fallback", []):
            p = self.personas[fb["persona"]]
            args = {"query": fb["query"], "fallback": True}
            if fb.get("scope"):
                args["scopes"] = [fb["scope"]]
            err, out, text = await self.call(p, "query_docs", **args)
            answered = not err and (any(r.get("answered") for r in out.get("results", [])) or source in out.get("fallback", {}))
            self.check(p.name, f"query_docs '{fb['query']}' answered from the index or fell back to {source}", answered,
                       f"fallback keys {list(out.get('fallback', {}))}" if not err else text[:200])

    # ---- driver
    async def run(self, names: list[str] | None = None) -> int:
        chosen = [self.personas[n] for n in (names or list(self.personas))]
        for p in chosen:
            self.out(f"\n{p.name} ({p.kind}; groups {p.groups or '-'}; scopes {p.grants.scopes}; banks {p.grants.banks})")
            for part in ("identity", "docs", "code", "memory"):
                if part in self.only:
                    try:
                        await getattr(self, part)(p)
                    except Exception as e:                       # a broken engine fails the part, not the run
                        self.check(p.name, f"{part} checks ran", False, f"{type(e).__name__}: {e}"[:300])
        if "probes" in self.only and self.probes.get("docs"):
            self.out("\nmarker probes (content isolation)")
            for probe in self.probes["docs"]:
                if names and not ({*probe.get("visible_to", []), *probe.get("hidden_from", [])} & set(names)):
                    continue
                await self.probe_docs(probe)
        if self.live and "live" in self.only and self.probes.get("live"):
            self.out("\nlive sources")
            await self.live_checks(self.probes["live"])
        return self.summary()

    def summary(self) -> int:
        failed = [r for r in self.results if not r.ok]
        self.out("\n" + "=" * 100)
        self.out(f"{'check':<62} {'persona':<10} result")
        for r in self.results:
            self.out(f"{r.part + ': ' + r.check:<62} {r.persona:<10} {'pass' if r.ok else 'FAIL'}")
        self.out("=" * 100)
        self.out(f"{len(self.results) - len(failed)} passed, {len(failed)} failed" + ("" if not failed else ":\n  " +
                 "\n  ".join(f"[{r.persona}] {r.check}: {r.detail}" for r in failed)))
        slow = sorted(self.timings, key=lambda t: -t[2])[:5]
        if slow:
            self.out("slowest calls: " + ", ".join(f"{t[1]}({t[0]}) {t[2]:.1f}s" for t in slow))
        return 1 if failed else 0


# ----------------------------------------------------------------------------------------------- cli
def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(prog="cerebro smoke", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default=os.environ.get("CEREBRO_CONFIG", "cerebro.yaml"))
    p.add_argument("--env-file", default="secrets.env", help="KEY=value file the ${NAME} references (tokens) resolve from")
    p.add_argument("--url", default=None, help="the MCP endpoint (default: gateway.host:port/path from the config)")
    p.add_argument("--as", dest="personas", action="append", help="persona subject (repeatable; default: every token)")
    p.add_argument("--token", default=None, help="bearer token for the single persona named with --as")
    p.add_argument("--probes", default=None, help="YAML with marker probes and live checks (tests/e2e/probes.yaml)")
    p.add_argument("--live", action="store_true", help="also run the live_search / live_fetch / fallback checks")
    p.add_argument("--only", default=",".join(PARTS), help=f"parts to run, comma separated: {','.join(PARTS)}")
    p.add_argument("--timeout", type=float, default=600, help="seconds per tool call")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    config = load_config(args.config, env={**read_env_file(args.env_file), **os.environ})
    personas = personas_from_config(config)
    names = args.personas or None
    if names:
        unknown = [n for n in names if n not in personas]
        if unknown:
            raise SystemExit(f"unknown persona(s) {unknown}; configured: {sorted(personas)}")
    if args.token:
        if not names or len(names) != 1:
            raise SystemExit("--token goes with exactly one --as <persona>")
        personas[names[0]].token = args.token
    url = args.url or config.resource_id()
    only = tuple(s.strip() for s in args.only.split(",") if s.strip())
    bad = [s for s in only if s not in PARTS]
    if bad:
        raise SystemExit(f"unknown part(s) {bad}; parts: {PARTS}")
    smoke = Smoke(config, personas, http_connect(url, args.timeout), probes=load_probes(args.probes), live=args.live,
                  only=only, verbose=args.verbose)
    print(f"cerebro smoke against {url}: personas {names or sorted(personas)}, parts {list(only)}"
          + (f", probes {args.probes}" if args.probes else "") + (", live" if args.live else ""))
    return asyncio.run(smoke.run(names))


if __name__ == "__main__":
    sys.exit(main())

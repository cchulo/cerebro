"""TokenSave code intelligence: one bridge unit per CodeUnit, reached through the provisioner's Locator.

The adapter never talks to TokenSave itself. It talks to the unit's bridge (cerebro.bridge, running inside
images/code-unit) over the wire contract: the capability manifest, /health, and MCP over streamable HTTP for
`grep` (the `search` capability) and the engine's read-only tools (the `graph` capability). What the provisioner
must create for each unit is described by `units()` and `jobs()`:

    unit  <unit.name>          image cerebro/code-unit:<version>  port 8045
                               args  cerebro bridge serve --unit <name> --workspace /workspace --port 8045
                               env   CEREBRO_UNIT, CEREBRO_REPOS (JSON: [{url, branches}]), CEREBRO_WORKSPACE
                               secret_env GITHUB_TOKEN (clone auth; only the job needs it, both get it)
                               volume workspace-<unit.name> at /workspace, shared with the job
    job   index-<unit.name>    same image and volume, args cerebro index run --unit <name>,
                               schedule engines.code.options.schedule (default "0 3 * * *")

TokenSave facts behind this (verified 2026-09-13, tokensave 7.12.1; see cerebro.bridge.engine and
cerebro.bridge.workspace for the sources): graphs are per repository and per tracked branch, selected per call
with graph_root / graph_branch, which the bridge derives from `repo` / `branch`; there is no text search in
TokenSave, so `search` is ripgrep in the unit.
"""
from __future__ import annotations
import datetime, json
from typing import Any
import httpx
from cerebro.core import CodeUnit, Health, code_units
from cerebro.core.contracts.code import Capabilities, CodeIntelligence, SearchHit, ToolInfo, ToolResult
from cerebro.core.contracts.provision import JobSpec, PortSpec, UnitSpec, VolumeSpec
from cerebro.core.types import Forbidden, Unsupported
from cerebro.bridge.workspace import REPOS_ENV, UNIT_ENV, repos_env_value

IMAGE = "cerebro/code-unit"
BUILD_DIR = "images/code-unit"
PORT = 8045
WORKSPACE = "/workspace"
DEFAULT_SCHEDULE = "0 3 * * *"


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("cerebro")
    except Exception:
        return "dev"


class Adapter(CodeIntelligence):
    name = "tokensave"

    def __init__(self, options=None, ctx=None):
        super().__init__(options, ctx)
        self.timeout = float(self.option("timeout", 120))
        self._caps: dict[str, Capabilities] = {}

    # ------------------------------------------------------------------ wiring
    def endpoint(self, unit: CodeUnit) -> str:
        return self.ctx.locator.endpoint(unit.name).rstrip("/")

    def invalidate(self, unit: CodeUnit | None = None) -> None:
        if unit is None:
            self._caps.clear()
        else:
            self._caps.pop(unit.name, None)

    async def capabilities(self, unit: CodeUnit) -> Capabilities:
        if unit.name in self._caps:
            return self._caps[unit.name]
        async with httpx.AsyncClient(timeout=self.timeout) as h:
            r = await h.get(f"{self.endpoint(unit)}/.well-known/cerebro-capabilities")
            r.raise_for_status()
            m = r.json()
        caps = m.get("capabilities", {})
        c = Capabilities(engine=m.get("engine", "tokensave"), version=m.get("version"),
                         search=bool(caps.get("search")), graph=bool(caps.get("graph")),
                         branches=bool(caps.get("branches")), multi_root=bool(caps.get("multi_root")),
                         tools=[ToolInfo(name=t["name"], description=t.get("description", ""),
                                         read_only=bool(t.get("read_only", True)), input_schema=t.get("input_schema"))
                                for t in m.get("tools", []) if t.get("read_only", True)])
        self._caps[unit.name] = c
        return c

    async def health(self, unit: CodeUnit) -> Health:
        try:
            async with httpx.AsyncClient(timeout=10) as h:
                r = await h.get(f"{self.endpoint(unit)}/health")
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 200 and data.get("ok", True):
                return Health.up(f"{unit.name}: {data.get('engine', 'engine')} {data.get('version') or ''}".strip(), **data)
            return Health.down(f"{unit.name}: bridge reports {r.status_code}", **data)
        except Exception as e:
            return Health.down(f"{unit.name}: {type(e).__name__}: {e}")

    # ------------------------------------------------------------------ contract
    async def search(self, unit: CodeUnit, query: str, *, repos: list[str] | None = None, branch: str | None = None,
                     regex: bool = False, max_results: int = 20) -> list[SearchHit]:
        caps = await self.capabilities(unit)
        if not caps.search:
            raise Unsupported(f"unit {unit.name} has no search capability")
        if branch and not caps.branches:
            raise Unsupported(f"unit {unit.name} ({caps.engine}) does not support branches")
        allowed = [r.name for r in unit.repos]
        if repos is not None:
            wanted = [r for r in allowed if r in set(repos)]              # intersection: never widens
            if not wanted:
                return []
        else:
            wanted = allowed
        res = await self._call_tool(unit, "grep", {"query": query, "repos": wanted, "branch": branch, "regex": regex,
                                                   "max_results": max_results})
        data = res.structured or _json_text(res.content) or {}
        if res.is_error:
            raise RuntimeError(f"grep failed on {unit.name}: {_text(res.content)[:300]}")
        hits = []
        for h in data.get("hits", []):
            if h.get("repository") in allowed:                           # belt and braces: only this unit's repos
                hits.append(SearchHit(**{k: h.get(k) for k in ("repository", "path", "line", "content", "language", "branch")}))
        return hits

    async def call(self, unit: CodeUnit, tool: str, args: dict[str, Any] | None = None, *,
                   branch: str | None = None) -> ToolResult:
        caps = await self.capabilities(unit)
        if tool not in caps.tool_names():
            raise Forbidden(f"tool '{tool}' is not exposed by unit {unit.name}; allowed: {sorted(caps.tool_names())}")
        if branch and not caps.branches:
            raise Unsupported(f"unit {unit.name} ({caps.engine}) does not support branches")
        args = dict(args or {})
        if branch:
            args["branch"] = branch
        if "repo" in args and args["repo"] not in {r.name for r in unit.repos} | {r.url for r in unit.repos}:
            raise Forbidden(f"repo '{args['repo']}' is not in unit {unit.name}")
        return await self._call_tool(unit, tool, args)

    async def _call_tool(self, unit: CodeUnit, tool: str, args: dict[str, Any]) -> ToolResult:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        async with streamablehttp_client(f"{self.endpoint(unit)}/mcp", timeout=self.timeout, sse_read_timeout=self.timeout) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool(tool, args, read_timeout_seconds=datetime.timedelta(seconds=self.timeout))
        return ToolResult(unit=unit.name, tool=tool, is_error=bool(res.isError),
                          content=[getattr(c, "text", None) or c.model_dump() for c in res.content],
                          structured=res.structuredContent)

    # ------------------------------------------------------------------ what to provision
    def _image(self) -> str:
        reg = self.ctx.config.provisioning.image_registry if self.ctx else None
        return f"{reg.rstrip('/')}/{IMAGE}:{_version()}" if reg else f"{IMAGE}:{_version()}"

    def _volume(self, unit: CodeUnit) -> VolumeSpec:
        eng = self.ctx.config.engines.code
        size = (eng.resources.get("storage") if eng else None) or "10Gi"
        return VolumeSpec(name=f"workspace-{unit.name}", mount_path=WORKSPACE, size=size,
                          shared_with=[unit.name, self.index_job_name(unit)])

    def _env(self, unit: CodeUnit) -> dict[str, str]:
        return {UNIT_ENV: unit.name, REPOS_ENV: repos_env_value(unit.repos), "CEREBRO_WORKSPACE": WORKSPACE,
                "TOKENSAVE_UPDATE_CHECK": "off"}

    def units(self) -> list[UnitSpec]:
        eng = self.ctx.config.engines.code
        out = []
        for unit in code_units(self.ctx.config):
            out.append(UnitSpec(
                name=unit.name, role="code", image=self._image(), build=BUILD_DIR,
                args=["cerebro", "bridge", "serve", "--unit", unit.name, "--workspace", WORKSPACE, "--port", str(PORT)],
                env=self._env(unit), secret_env=["GITHUB_TOKEN"], ports=[PortSpec(name="http", port=PORT)],
                volumes=[self._volume(unit)], resources={k: v for k, v in (eng.resources if eng else {}).items() if k != "storage"},
                health_path="/health", scope=unit.scope, idle_ttl=eng.idle_ttl if eng else None, depends_on=[],
                labels={"cerebro.io/engine": "tokensave", "cerebro.io/unit-kind": unit.kind}))
        return out

    def jobs(self) -> list[JobSpec]:
        schedule = str(self.option("schedule", DEFAULT_SCHEDULE))
        out = []
        for unit in code_units(self.ctx.config):
            out.append(JobSpec(
                name=self.index_job_name(unit), image=self._image(), build=BUILD_DIR,
                args=["cerebro", "index", "run", "--unit", unit.name], env=self._env(unit), secret_env=["GITHUB_TOKEN"],
                volumes=[self._volume(unit)], schedule=schedule, scope=unit.scope, depends_on=[],
                labels={"cerebro.io/engine": "tokensave", "cerebro.io/unit": unit.name}))
        return out


def _text(content: list[Any]) -> str:
    return "\n".join(c for c in content if isinstance(c, str))


def _json_text(content: list[Any]) -> dict | None:
    for c in content:
        if isinstance(c, str) and c.lstrip().startswith("{"):
            try:
                return json.loads(c)
            except ValueError:
                continue
    return None

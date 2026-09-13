"""The bridge process: one stdio engine behind a stateless streamable-HTTP MCP endpoint plus health and manifest.

    Bridge(unit, workspace, engine)      start() spawns the engine and computes the tool list; call() proxies
    build_app(bridge) -> Starlette       GET /health, GET /.well-known/cerebro-capabilities, POST /mcp

Proxied tools carry `repo` / `branch` instead of the engine's selectors. `repo` omitted on a unit with several
repositories fans the call out to every repository (the manifest declares `multi_root`), one content block per
repository. Engine errors (JSON-RPC errors from a bad selector, a missing argument) become tool results with
`isError` so the gateway sees a tool error, never a broken transport.
"""
from __future__ import annotations
import asyncio, contextlib, datetime, json, logging, sys
from typing import Any
import anyio
from mcp import types as mt, ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import McpError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from .engine import StdioEngine, REPO_ARG, BRANCH_ARG
from .grep import grep
from .workspace import Workspace

log = logging.getLogger("cerebro.bridge")
VERSION = "2"
GREP_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "text to find (a fixed string unless regex=true)"},
        "repos": {"type": "array", "items": {"type": "string"},
                  "description": "repositories to search (github.com/org/x); default: every repository of the unit"},
        "branch": {"type": "string", "description": "indexed branch to search; default: each repository's default branch"},
        "regex": {"type": "boolean", "default": False},
        "max_results": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200},
        "context": {"type": "integer", "default": 1, "minimum": 0, "maximum": 10},
    },
    "required": ["query"],
}


class Bridge:
    def __init__(self, unit: str, workspace: Workspace, engine: StdioEngine, *, concurrency: int = 8,
                 call_timeout: float = 120.0):
        self.unit, self.workspace, self.engine = unit, workspace, engine
        self.call_timeout = call_timeout
        self._sem = asyncio.Semaphore(concurrency)
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._session: ClientSession | None = None
        self._engine_tools: dict[str, mt.Tool] = {}
        self.exposed: dict[str, mt.Tool] = {}          # engine tool name -> public Tool (rewritten schema)
        self.engine_version: str | None = None
        self.started_at: str | None = None
        self.restarts = 0

    # ------------------------------------------------------------------ engine process
    # The stdio session (a process + anyio task group) is owned by ONE supervisor task: anyio cancel scopes must be
    # entered and exited by the same task, so requests never touch the exit stack; they only use the session.
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.engine.ensure_ready)
        self.engine_version = await loop.run_in_executor(None, self.engine.version)
        await self._spawn()
        self.started_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    async def _spawn(self) -> None:
        ready: asyncio.Future = asyncio.get_running_loop().create_future()
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._supervise(ready, self._stop_event), name=f"engine-{self.unit}")
        await ready

    async def _supervise(self, ready: asyncio.Future, stop_event: asyncio.Event) -> None:
        argv = self.engine.command()
        params = StdioServerParameters(command=argv[0], args=argv[1:], env=self.engine.env(), cwd=str(self.engine.cwd()))
        try:
            async with stdio_client(params, errlog=sys.stderr) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), 120)
                    tools = (await session.list_tools()).tools
                    self._engine_tools = {t.name: t for t in tools}
                    self.exposed = {t.name: mt.Tool(name=t.name, title=t.title, description=t.description,
                                                    inputSchema=self.engine.public_schema(t),
                                                    annotations=mt.ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                                                                   idempotentHint=True, openWorldHint=False))
                                    for t in tools if self.engine.exposes(t)}
                    self._session = session
                    log.info("bridge %s: engine %s %s up, %d/%d tools exposed", self.unit, self.engine.name,
                             self.engine_version, len(self.exposed), len(tools))
                    ready.set_result(None)
                    await stop_event.wait()
        except BaseException as e:
            if not ready.done():
                ready.set_exception(e if isinstance(e, Exception) else RuntimeError(f"engine start cancelled: {e!r}"))
            if isinstance(e, asyncio.CancelledError):
                raise
            log.warning("bridge %s: engine session ended: %r", self.unit, e)
        finally:
            self._session = None

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(task, 30)
        except (asyncio.TimeoutError, Exception):
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    async def _restart(self) -> None:
        async with self._lock:
            if self._session is not None:
                return                                   # another request already brought it back
            self.restarts += 1
            log.warning("bridge %s: restarting engine (%d)", self.unit, self.restarts)
            await self.stop()
            await self._spawn()

    @property
    def alive(self) -> bool:
        return self._session is not None

    # ------------------------------------------------------------------ tool surface
    def tools(self) -> list[mt.Tool]:
        own = [mt.Tool(name="grep", description="Text search inside this unit's repositories (ripgrep; git grep on "
                                                "a non-default indexed branch). Returns hits with repository, "
                                                "path, line, content, language, branch.",
                       inputSchema=GREP_SCHEMA, annotations=mt.ToolAnnotations(readOnlyHint=True)),
               mt.Tool(name="unit_info", description="This unit's manifest: engine, repositories, default and "
                                                     "indexed branches, exposed tools.",
                       inputSchema={"type": "object", "properties": {}},
                       annotations=mt.ToolAnnotations(readOnlyHint=True))]
        return own + list(self.exposed.values())

    def manifest(self) -> dict[str, Any]:
        return {"engine": self.engine.name, "version": self.engine_version, "unit": self.unit, "bridge": VERSION,
                "served_root": str(self.workspace.served_root),
                "capabilities": {"search": True, "graph": bool(self.exposed),
                                 "branches": self.engine.branch_param is not None, "multi_root": True},
                "tools": [{"name": t.name, "description": t.description or "", "read_only": True,
                           "input_schema": t.inputSchema} for t in self.tools()],
                "repos": self.workspace.describe()}

    def health(self) -> dict[str, Any]:
        repos = self.workspace.describe()
        return {"ok": self.alive, "unit": self.unit, "engine": self.engine.name, "version": self.engine_version,
                "engine_alive": self.alive, "restarts": self.restarts, "started_at": self.started_at,
                "repos": len(repos), "indexed_repos": sum(1 for r in repos if r["indexed"]),
                **self.engine.health()}

    # ------------------------------------------------------------------ calls
    async def call(self, name: str, args: dict[str, Any] | None) -> mt.CallToolResult:
        args = dict(args or {})
        try:
            if name == "grep":
                res = await grep(self.workspace, args.pop("query", ""), repos=args.get("repos"),
                                 branch=args.get("branch"), regex=bool(args.get("regex", False)),
                                 max_results=int(args.get("max_results", 20)), context=int(args.get("context", 1)))
                return _ok(res)
            if name == "unit_info":
                return _ok(self.manifest())
            if name not in self.exposed:
                return _error(f"tool '{name}' is not exposed by this unit; available: {[t.name for t in self.tools()]}")
            return await self._proxy(name, args)
        except ValueError as e:
            return _error(str(e))
        except Exception as e:                       # never let a transport error out of the tool handler
            log.exception("bridge %s: %s failed", self.unit, name)
            return _error(f"{type(e).__name__}: {e}")

    async def _proxy(self, name: str, args: dict[str, Any]) -> mt.CallToolResult:
        repo_ref, branch = args.get(REPO_ARG), args.get(BRANCH_ARG)
        try:
            targets = [self.workspace.entry(repo_ref)] if repo_ref else list(self.workspace.repos)
        except KeyError as e:
            return _error(str(e))
        if not targets:
            return _error("this unit has no repositories")
        plans = []
        for repo in targets:
            if not self.engine.indexed(self.workspace.path(repo)):
                plans.append((repo, None, f"{repo.name}: not indexed yet"))
                continue
            try:
                ref = self.workspace.resolve_branch(repo, branch)
            except ValueError as e:
                plans.append((repo, None, str(e)))
                continue
            plans.append((repo, self.engine.prepare(args, self.workspace.path(repo), ref), None))
        if len(plans) == 1:
            repo, prepared, err = plans[0]
            return _error(err) if err else await self._engine_call(name, prepared)
        results = await asyncio.gather(*[self._engine_call(name, p) if p is not None else _aerr(e) for _, p, e in plans])
        content: list[mt.ContentBlock] = []
        structured: list[dict[str, Any]] = []
        for (repo, _, _), res in zip(plans, results):
            content.append(mt.TextContent(type="text", text=f"### repository {repo.name}" + (f" @ {branch}" if branch else "")))
            content.extend(res.content)
            structured.append({"repository": repo.name, "branch": branch, "is_error": bool(res.isError),
                               "structured": res.structuredContent,
                               "text": [c.text for c in res.content if isinstance(c, mt.TextContent)]})
        return mt.CallToolResult(content=content, structuredContent={"results": structured},
                                 isError=all(r.isError for r in results))

    async def _engine_call(self, name: str, prepared: dict[str, Any]) -> mt.CallToolResult:
        for attempt in (1, 2):
            session = self._session
            if session is None:
                await self._restart()
                session = self._session
            try:
                async with self._sem:
                    return await session.call_tool(name, prepared,
                                                   read_timeout_seconds=datetime.timedelta(seconds=self.call_timeout))
            except McpError as e:
                return _error(str(e.error.message if hasattr(e, "error") else e))
            except (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream, ConnectionError) as e:
                if attempt == 2:
                    return _error(f"engine unavailable: {e}")
                self._session = None
                await self._restart()
        return _error("engine unavailable")


async def _aerr(msg: str) -> mt.CallToolResult:
    return _error(msg)


def _ok(data: dict[str, Any]) -> mt.CallToolResult:
    return mt.CallToolResult(content=[mt.TextContent(type="text", text=json.dumps(data, indent=2))],
                             structuredContent=data)


def _error(msg: str) -> mt.CallToolResult:
    return mt.CallToolResult(content=[mt.TextContent(type="text", text=msg)], isError=True)


# ---------------------------------------------------------------------- ASGI app
def build_app(bridge: Bridge, *, json_response: bool = True) -> Starlette:
    server: Server = Server(f"cerebro-bridge-{bridge.unit}", version=VERSION,
                            instructions=f"cerebro code unit {bridge.unit}: read-only {bridge.engine.name} tools "
                                         "plus grep and unit_info. Pass repo/branch to address one repository.")

    @server.list_tools()
    async def _list_tools() -> list[mt.Tool]:
        return bridge.tools()

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any]) -> mt.CallToolResult:
        return await bridge.call(name, arguments)

    manager = StreamableHTTPSessionManager(app=server, json_response=json_response, stateless=True)

    async def health(_: Request) -> JSONResponse:
        h = bridge.health()
        return JSONResponse(h, status_code=200 if h["ok"] else 503)

    async def manifest(_: Request) -> JSONResponse:
        return JSONResponse(bridge.manifest())

    class McpEndpoint:                           # a raw ASGI endpoint: no trailing-slash redirect, no Request wrapping
        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        await bridge.start()
        try:
            async with manager.run():
                yield
        finally:
            await bridge.stop()

    return Starlette(routes=[Route("/health", health), Route("/.well-known/cerebro-capabilities", manifest),
                             Route("/mcp", McpEndpoint(), methods=["GET", "POST", "DELETE"])], lifespan=lifespan)

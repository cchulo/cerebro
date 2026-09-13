"""Shared helpers: a temporary /workspace with real git repositories, and the bridge running in-process."""
from __future__ import annotations
import asyncio, contextlib, json, pathlib, socket, subprocess
from cerebro.bridge.engine import StdioEngine
from cerebro.bridge.server import Bridge, build_app
from cerebro.bridge.workspace import RepoEntry, RepoState, Workspace, entries_from_specs
from cerebro.core.config import RepoSpec


def git(cwd: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(ws_root: pathlib.Path, dirname: str, files: dict[str, str], branches: dict[str, dict[str, str]] | None = None,
              default: str = "main", indexed: bool = True, url: str | None = None, name: str | None = None) -> pathlib.Path:
    """A git repo at ws_root/dirname with `files` on `default`; each extra branch adds/overwrites `files`.
    Leaves `default` checked out and writes the indexer's state file (plus a `.tokensave` marker) when indexed."""
    d = ws_root / dirname
    d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", default, str(d)], check=True)
    git(d, "config", "user.email", "t@example.com"); git(d, "config", "user.name", "t")
    for path, text in files.items():
        (d / path).parent.mkdir(parents=True, exist_ok=True); (d / path).write_text(text)
    git(d, "add", "."); git(d, "commit", "-qm", "init")
    shas = {default: git(d, "rev-parse", "HEAD")}
    for b, extra in (branches or {}).items():
        git(d, "checkout", "-q", "-b", b, default)
        for path, text in extra.items():
            (d / path).parent.mkdir(parents=True, exist_ok=True); (d / path).write_text(text)
        git(d, "add", "."); git(d, "commit", "-qm", f"on {b}")
        shas[b] = git(d, "rev-parse", "HEAD")
        git(d, "checkout", "-q", default)
    if indexed:
        (d / ".tokensave").mkdir()
        (d / ".tokensave" / "branch-meta.json").write_text(json.dumps(
            {"default_branch": default, "branches": {b: {"db_file": "x"} for b in shas}}))
        st = RepoState(url=url or f"https://github.com/acme/{dirname}.git", name=name or f"github.com/acme/{dirname}",
                       default_branch=default, branches=shas)
        (d / ".cerebro-index.json").write_text(st.model_dump_json(indent=2))
    return d


def entries(*specs: tuple[str, list[str]]) -> list[RepoEntry]:
    return entries_from_specs([RepoSpec(url=u, branches=b) for u, b in specs])


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.asynccontextmanager
async def running_bridge(unit: str, ws: Workspace, engine: StdioEngine):
    """The real ASGI app on a free loopback port, engine spawned by its lifespan. Yields (bridge, base_url)."""
    import uvicorn
    bridge = Bridge(unit, ws, engine)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(build_app(bridge), host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    for _ in range(400):
        if server.started or task.done():
            break
        await asyncio.sleep(0.05)
    if task.done():
        task.result()
    try:
        yield bridge, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 30)

"""A tiny stdio MCP engine for bridge tests: two read-only selector-capable tools, one read-only tool without a
root selector (must be hidden), one write tool (must be hidden), one that fails. Every tool echoes its arguments
so tests can prove the bridge injected `graph_root` / `graph_branch` and stripped `repo` / `branch`.

Run as a script it serves stdio; imported it provides `FakeEngine` (a cerebro.bridge.engine.StdioEngine)."""
from __future__ import annotations
import sys
from typing import Any
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

RO = ToolAnnotations(readOnlyHint=True)
RW = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

mcp = FastMCP("fake-engine")


@mcp.tool(annotations=RO)
def fake_search(query: str, limit: int = 10, graph_root: str | None = None, graph_branch: str | None = None) -> dict[str, Any]:
    """Find symbols by name (fake)."""
    return {"tool": "fake_search", "args": {"query": query, "limit": limit, "graph_root": graph_root,
                                            "graph_branch": graph_branch}}


@mcp.tool(annotations=RO)
def fake_status(graph_root: str | None = None, graph_branch: str | None = None) -> dict[str, Any]:
    """Index status (fake)."""
    return {"tool": "fake_status", "args": {"graph_root": graph_root, "graph_branch": graph_branch}}


@mcp.tool(annotations=RO)
def fake_fail(graph_root: str | None = None, graph_branch: str | None = None) -> dict[str, Any]:
    """Always fails (fake)."""
    raise ValueError("boom from the engine")


@mcp.tool(annotations=RO)
def fake_local_only() -> str:
    """Read-only but answers about the served project only (no selector): must not be exposed."""
    return "served project"


@mcp.tool(annotations=RW)
def fake_write(path: str, content: str) -> str:
    """Writes a file: must never be exposed."""
    return "written"


try:
    from cerebro.bridge.engine import StdioEngine

    class FakeEngine(StdioEngine):
        name = "fake"
        binary = sys.executable
        index_marker = ".tokensave"

        def command(self) -> list[str]:
            return [sys.executable, __file__]

        def version(self) -> str | None:
            return "0.0-fake"

        def ensure_ready(self) -> None:
            self.workspace.served_root.mkdir(parents=True, exist_ok=True)
except ImportError:      # running as the stdio script without cerebro importable is fine too
    pass


if __name__ == "__main__":
    mcp.run("stdio")

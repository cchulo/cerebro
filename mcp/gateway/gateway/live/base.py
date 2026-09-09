"""Live sources: fetch straight from a system of record when the index misses, under the same scopes.

A live source is the query-time twin of an ingest adapter. The gateway hands it `allowed`: one entry per scope the
caller may read that lists this source under `docs:` in config/scopes.yaml, e.g.
    [{"scope": "public", "spaces": ["ENG", "DOCS"]}, {"scope": "payments", "spaces": ["PAY"]}]
and the source must confine every search and fetch to exactly that. It may call REST APIs or an upstream MCP
server (Atlassian's, mcp-atlassian, ...) internally; the constraint is applied here, not trusted to the upstream.

Rules:
- search() only returns items inside `allowed`; fetch() refuses (PermissionError) anything outside it.
- Apply the same finer-ACL rule as the ingest: a page with its own read restriction is never returned.
- Credentials come from the environment (`self.env`), non-secret options from `live:` in scopes.yaml (`self.option`).
- Keep results small (excerpts, bounded text); this sits inside an agent's context window.
"""
import os


class LiveSource:
    name: str = "base"

    def __init__(self, options: dict | None = None):
        self.options = options or {}

    def env(self, key: str, default: str = "") -> str:
        return os.environ.get(key, default)

    def option(self, key: str, default=None):
        return self.options.get(key, default)

    def configured(self) -> bool:
        return True

    async def search(self, query: str, allowed: list[dict], limit: int = 10) -> list[dict]:
        """Return [{"ref": ..., "title": ..., "scope": ..., "url": ..., "excerpt": ...}] inside `allowed` only."""
        raise NotImplementedError

    async def fetch(self, ref: str, allowed: list[dict], max_chars: int = 20000) -> dict:
        """Return {"ref", "title", "scope", "url", "text"} or raise PermissionError if outside `allowed`."""
        raise NotImplementedError

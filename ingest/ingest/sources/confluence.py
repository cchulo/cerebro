"""Confluence Cloud/Server adapter (REST API v1: GET /rest/api/content).

Scope config:  docs: { confluence: { spaces: [ENG, DOCS] } }
Env:           CONFLUENCE_URL, CONFLUENCE_USER, CONFLUENCE_TOKEN
Webhook filter: {"space": "ENG"}

Pages with page-level read restrictions are skipped: the space's scope is not a valid ACL for them and a graph
index cannot filter per page afterwards (docs/ACCESS-CONTROL.md).
"""
import httpx
from markdownify import markdownify
from ..config import CONFLUENCE_URL, CONFLUENCE_USER, CONFLUENCE_TOKEN
from .base import Source, Document, ScopeContext


class ConfluenceSource(Source):
    name = "confluence"

    def configured(self) -> bool:
        return bool(CONFLUENCE_URL and CONFLUENCE_TOKEN)

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=CONFLUENCE_URL, auth=(CONFLUENCE_USER, CONFLUENCE_TOKEN), timeout=60)

    @staticmethod
    def _pages(c: httpx.Client, space: str):
        start = 0
        while True:
            r = c.get("/rest/api/content", params={
                "spaceKey": space, "type": "page", "status": "current",
                "expand": "body.storage,version,ancestors,restrictions.read.restrictions.user,restrictions.read.restrictions.group",
                "limit": 50, "start": start})
            r.raise_for_status()
            data = r.json()
            yield from data.get("results", [])
            if "next" not in data.get("_links", {}):
                return
            start += data.get("limit", 50)

    @staticmethod
    def _restricted(p: dict) -> bool:
        read = p.get("restrictions", {}).get("read", {}).get("restrictions", {})
        return bool(read.get("user", {}).get("results") or read.get("group", {}).get("results"))

    def documents(self, ctx: ScopeContext, filter: dict | None = None):
        spaces = ctx.config.get("spaces", [])
        if filter and filter.get("space"):
            spaces = [s for s in spaces if s == filter["space"]]
        with self._client() as c:
            for space in spaces:
                for p in self._pages(c, space):
                    if self._restricted(p):
                        continue
                    crumbs = " / ".join(a["title"] for a in p.get("ancestors", []))
                    body = markdownify(p["body"]["storage"]["value"], heading_style="ATX")
                    header = (f"Space: {space}\nPath: {crumbs + ' / ' if crumbs else ''}{p['title']}\n"
                              f"URL: {CONFLUENCE_URL}{p.get('_links', {}).get('webui', '')}\n\n")
                    yield Document(key=f"{space}/{p['id']}", version=str(p["version"]["number"]),
                                   text=header + body, title=p["title"])

    def covers(self, key: str, filter: dict | None) -> bool:
        if not filter:
            return True
        return bool(filter.get("space")) and key.startswith(filter["space"] + "/")

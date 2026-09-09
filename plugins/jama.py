"""Jama Connect adapter (requirements / test cases / any item type) - a complete plugin example.

Written against the Jama Connect REST API v1 (GET /rest/v1/items?project=<id>&startAt=&maxResults=, GET /rest/v1/abstractitems);
NOT yet verified against a live Jama instance: run `make source-check SCOPE=<scope> SOURCE=jama` first and adjust
field names to your version.

Env:            JAMA_URL (e.g. https://jama.example.com), JAMA_USER + JAMA_TOKEN (basic auth / API token)
sources: option jama: { url: ..., page_size: 50 }              (optional)
Scope config:   docs: { jama: { projects: [42, 57], item_types: [89] } }   item_types optional (numeric type ids)
Webhook filter: {"project": 42}
"""
import hashlib, html, re
import httpx
from ingest.sources import Source, Document, ScopeContext


def _strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", s or "", flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


class JamaSource(Source):
    name = "jama"

    def __init__(self, options=None):
        super().__init__(options)
        self.url = (self.option("url") or self.env("JAMA_URL")).rstrip("/")
        self.user, self.token = self.env("JAMA_USER"), self.env("JAMA_TOKEN")
        self.page_size = int(self.option("page_size", 50))

    def configured(self) -> bool:
        return bool(self.url and self.token)

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=f"{self.url}/rest/v1", auth=(self.user, self.token), timeout=60)

    def _items(self, c: httpx.Client, project: int, item_types: list[int]):
        start = 0
        while True:
            r = c.get("/items", params={"project": project, "startAt": start, "maxResults": self.page_size})
            r.raise_for_status()
            body = r.json()
            data = body.get("data", [])
            for it in data:
                if item_types and it.get("itemType") not in item_types:
                    continue
                yield it
            page = body.get("meta", {}).get("pageInfo", {})
            start += len(data)
            if not data or start >= page.get("totalResults", 0):
                return

    def documents(self, ctx: ScopeContext, filter: dict | None = None):
        projects = [int(p) for p in ctx.config.get("projects", [])]
        if filter and filter.get("project"):
            projects = [p for p in projects if p == int(filter["project"])]
        item_types = [int(t) for t in ctx.config.get("item_types", [])]
        with self._client() as c:
            for project in projects:
                for it in self._items(c, project, item_types):
                    f = it.get("fields", {})
                    key = it.get("documentKey") or str(it["id"])
                    lines = [f"Jama item {key} (project {project}, type {it.get('itemType')})",
                             f"Name: {f.get('name', '')}",
                             f"Status: {f.get('status', '')}   Modified: {it.get('modifiedDate', '')}",
                             f"URL: {self.url}/perspective.req#/items/{it['id']}", ""]
                    for fld in ("description", "notes", "rationale", "acceptanceCriteria"):
                        if f.get(fld):
                            lines.append(f"{fld.capitalize()}:\n{_strip_html(str(f[fld]))}\n")
                    text = "\n".join(lines)
                    version = it.get("modifiedDate") or hashlib.sha256(text.encode()).hexdigest()[:16]
                    yield Document(key=f"{project}/{key}", version=str(version), text=text, title=f.get("name", key))

    def covers(self, key: str, filter: dict | None) -> bool:
        if not filter:
            return True
        return bool(filter.get("project")) and key.startswith(f"{int(filter['project'])}/")

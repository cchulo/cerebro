"""Backstage software catalog adapter: every entity becomes a short structured document and its relations
(ownedBy, partOf, dependsOn, providesApi, consumesApi, ...) are written as explicit sentences so LightRAG's
entity/relation extraction picks them up cleanly.

Scope config:   docs: { backstage: {} }                     whole catalog (org-wide metadata)
                docs: { backstage: { kinds: [Component, API] } }
Env:            BACKSTAGE_URL, BACKSTAGE_TOKEN (optional)
sources: option backstage: { url: ... }   (optional, overrides BACKSTAGE_URL)
"""
import hashlib
import httpx
from ingest.sources import Source, Document, ScopeContext

DEFAULT_KINDS = ["Component", "System", "API", "Domain", "Resource", "Group"]


class BackstageSource(Source):
    name = "backstage"

    def __init__(self, options=None):
        super().__init__(options)
        self.url = (self.option("url") or self.env("BACKSTAGE_URL")).rstrip("/")
        self.token = self.env("BACKSTAGE_TOKEN")

    def configured(self) -> bool:
        return bool(self.url)

    def _entities(self, kinds):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        with httpx.Client(base_url=self.url, headers=headers, timeout=60) as c:
            for kind in kinds:
                r = c.get("/api/catalog/entities", params={"filter": f"kind={kind}"})
                r.raise_for_status()
                yield from r.json()

    @staticmethod
    def render(e: dict) -> str:
        md, spec, rels = e["metadata"], e.get("spec", {}), e.get("relations", [])
        ref = f"{e['kind'].lower()}:{md.get('namespace', 'default')}/{md['name']}"
        lines = [f"Backstage {e['kind']}: {md['name']}",
                 f"Entity ref: {ref}",
                 f"Description: {md.get('description', '')}",
                 f"Type: {spec.get('type', '')}   Lifecycle: {spec.get('lifecycle', '')}",
                 f"Owner: {spec.get('owner', '')}",
                 f"Tags: {', '.join(md.get('tags', []))}"]
        lines += [f"{md['name']} {r['type']} {r['targetRef']}." for r in rels]
        lines += [f"Link: {l.get('title', '')} {l.get('url', '')}" for l in md.get("links", [])]
        return "\n".join(lines)

    def documents(self, ctx: ScopeContext, filter: dict | None = None):
        for e in self._entities(ctx.config.get("kinds", DEFAULT_KINDS)):
            md = e["metadata"]
            text = self.render(e)
            yield Document(key=f"{e['kind'].lower()}/{md.get('namespace', 'default')}/{md['name']}",
                           version=hashlib.sha256(text.encode()).hexdigest()[:16],
                           text=text, title=f"{e['kind']} {md['name']}")

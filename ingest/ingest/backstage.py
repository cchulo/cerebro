"""Backstage catalog sync: every entity becomes a short structured document, and its
relations (ownedBy, partOf, dependsOn, providesApi, consumesApi) are written out as
explicit sentences so LightRAG's entity/relation extraction picks them up cleanly."""
import hashlib, json
import httpx
from . import state, lightrag, scopes
from .config import BACKSTAGE_URL, BACKSTAGE_TOKEN

KINDS = ["Component", "System", "API", "Domain", "Resource", "Group"]

def _entities():
    headers = {"Authorization": f"Bearer {BACKSTAGE_TOKEN}"} if BACKSTAGE_TOKEN else {}
    with httpx.Client(base_url=BACKSTAGE_URL, headers=headers, timeout=60) as c:
        for kind in KINDS:
            r = c.get("/api/catalog/entities", params={"filter": f"kind={kind}"})
            r.raise_for_status()
            yield from r.json()

def _render(e: dict) -> str:
    md, spec, rels = e["metadata"], e.get("spec", {}), e.get("relations", [])
    ref = f"{e['kind'].lower()}:{md.get('namespace','default')}/{md['name']}"
    lines = [f"Backstage {e['kind']}: {md['name']}",
             f"Entity ref: {ref}",
             f"Description: {md.get('description','')}",
             f"Type: {spec.get('type','')}   Lifecycle: {spec.get('lifecycle','')}",
             f"Owner: {spec.get('owner','')}",
             f"Tags: {', '.join(md.get('tags', []))}"]
    for r in rels:
        lines.append(f"{md['name']} {r['type']} {r['targetRef']}.")
    if "links" in md:
        lines += [f"Link: {l.get('title','')} {l.get('url','')}" for l in md["links"]]
    return "\n".join(lines)

def sync() -> dict:
    if not BACKSTAGE_URL:
        return {"skipped": "backstage not configured"}
    targets = scopes.backstage_scopes()      # catalog is org-wide metadata; goes to every scope flagged backstage: true
    if not targets:
        return {"skipped": "no scope has backstage: true"}
    batches, new_state, drop = lightrag.Batches(), {}, set()
    changed, seen = 0, set()
    for e in _entities():
        md = e["metadata"]
        key = f"backstage:{e['kind'].lower()}:{md.get('namespace','default')}/{md['name']}"
        seen.add(key)
        text = _render(e)
        version = hashlib.sha256(text.encode()).hexdigest()[:16]
        if state.get(key) == version:
            continue
        for scope in targets:
            batches[scope].upsert(key, text, title=f"{e['kind']} {md['name']}")
        new_state[key] = version; changed += 1
    removed = 0
    for key in state.keys_with_prefix("backstage:"):
        if key not in seen:
            for scope in targets:
                batches[scope].delete(key)
            drop.add(key); removed += 1
    flushed = batches.flush()
    state.commit(new_state, drop)
    return {"changed": changed, "removed": removed, "lightrag": flushed}

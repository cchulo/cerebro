"""Confluence Cloud sync, scope-aware.
- Each space is routed to exactly one scope (config/scopes.yaml); unlisted spaces are never ingested.
- Pages with read restrictions are skipped by default (restricted_pages: skip): a page-level restriction means
  the space's scope is NOT a valid ACL for it, and a graph index cannot filter per page after the fact.
"""
import httpx
from markdownify import markdownify
from . import state, lightrag, scopes
from .config import CONFLUENCE_URL, CONFLUENCE_USER, CONFLUENCE_TOKEN

def _client() -> httpx.Client:
    return httpx.Client(base_url=CONFLUENCE_URL, auth=(CONFLUENCE_USER, CONFLUENCE_TOKEN), timeout=60)

def _iter_pages(c: httpx.Client, space: str):
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
            break
        start += data["limit"]

def _is_restricted(p: dict) -> bool:
    read = p.get("restrictions", {}).get("read", {}).get("restrictions", {})
    return bool(read.get("user", {}).get("results") or read.get("group", {}).get("results"))

def sync(space_filter: list[str] | None = None) -> dict:
    if not (CONFLUENCE_URL and CONFLUENCE_TOKEN):
        return {"skipped": "confluence not configured"}
    batches, new_state, drop = lightrag.Batches(), {}, set()
    changed, skipped_restricted, seen = 0, 0, set()
    spaces = [(sp, sc) for sp, sc in scopes.all_spaces() if not space_filter or sp in space_filter]
    with _client() as c:
        for space, scope in spaces:
            for p in _iter_pages(c, space):
                key = f"confluence:{space}:{p['id']}"
                if _is_restricted(p):
                    if scopes.RESTRICTED_PAGES == "skip":
                        skipped_restricted += 1
                        if state.get(key):                 # was public before, now restricted -> remove
                            batches[scope].delete(key); drop.add(key)
                        continue
                    scope = f"restricted-{p['id']}"        # own_scope mode (needs a matching instance)
                seen.add(key)
                version = str(p["version"]["number"])
                if state.get(key) == version:
                    continue
                crumbs = " / ".join(a["title"] for a in p.get("ancestors", []))
                md = markdownify(p["body"]["storage"]["value"], heading_style="ATX")
                header = f"Space: {space}\nPath: {crumbs} / {p['title']}\nURL: {CONFLUENCE_URL}{p['_links']['webui']}\n\n"
                batches[scope].upsert(key, header + md, title=p["title"])
                new_state[key] = version; changed += 1
    removed = 0
    for key in state.keys_with_prefix("confluence:"):
        space = key.split(":")[1]
        if key not in seen and (not space_filter or space in space_filter):
            scope = scopes.scope_for_space(space)
            if scope:
                batches[scope].delete(key)
            drop.add(key); removed += 1
    flushed = batches.flush()                              # raises before state is touched if LightRAG refused
    state.commit(new_state, drop)
    return {"changed": changed, "removed": removed, "skipped_restricted": skipped_restricted, "lightrag": flushed}

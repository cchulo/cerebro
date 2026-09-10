"""Tiny stand-ins for the two REST APIs the built-in adapters call.

MOCK=confluence : GET /rest/api/content?spaceKey=..&start=..&limit=..   (Confluence REST v1 shape)
MOCK=backstage  : GET /api/catalog/entities?filter=kind=..              (Backstage catalog shape)
Fixtures: /fixtures/confluence.yaml, /fixtures/backstage.yaml
"""
import os, re, yaml
from fastapi import FastAPI, HTTPException, Query

MODE = os.environ.get("MOCK", "confluence")
_FIX = {"mtime": None, "data": None}

def fix():
    """The fixture file, re-read whenever it changes on disk (scripts/demo.sh add-page edits it while running)."""
    path = f"/fixtures/{MODE}.yaml"
    mtime = os.stat(path).st_mtime
    if _FIX["mtime"] != mtime:
        with open(path) as f:
            _FIX["data"] = yaml.safe_load(f)
        _FIX["mtime"] = mtime
    return _FIX["data"]
app = FastAPI(title=f"mock-{MODE}")


@app.get("/health")
def health():
    return {"ok": True, "mock": MODE}


if MODE == "confluence":
    @app.get("/rest/api/content")
    def content(spaceKey: str, start: int = 0, limit: int = 50, type: str = "page", status: str = "current",
                expand: str = ""):
        pages = fix().get("spaces", {}).get(spaceKey, [])
        out = []
        for p in pages:
            restr = p.get("restricted_to_groups", [])
            out.append({
                "id": str(p["id"]), "type": "page", "status": "current", "title": p["title"],
                "version": {"number": p.get("version", 1)},
                "ancestors": [{"title": a} for a in p.get("ancestors", [])],
                "body": {"storage": {"value": p["body"], "representation": "storage"}},
                "restrictions": {"read": {"restrictions": {
                    "user": {"results": []},
                    "group": {"results": [{"type": "group", "name": g} for g in restr]}}}},
                "_links": {"webui": f"/spaces/{spaceKey}/pages/{p['id']}"},
            })
        page = out[start:start + limit]
        links = {"self": "/rest/api/content"}
        if start + limit < len(out):
            links["next"] = f"/rest/api/content?spaceKey={spaceKey}&start={start + limit}&limit={limit}"
        return {"results": page, "start": start, "limit": limit, "size": len(page), "_links": links}

    def _all_pages():
        for sk in fix().get("spaces", {}):
            for p in content(sk, 0, 10000)["results"]:
                p["space"] = {"key": sk}
                yield p

    @app.get("/rest/api/content/search")
    def search(cql: str, limit: int = 25, expand: str = ""):
        # understands:  type=page AND space in ("A","B") AND text ~ "words"
        spaces = re.findall(r'"([^"]+)"', cql.split("space in", 1)[1].split(")", 1)[0]) if "space in" in cql else None
        extra = re.findall(r"space\s*=\s*\"?([A-Za-z0-9_-]+)\"?", cql)          # spaces_filter: (space = ENG OR space = DOCS)
        if extra:
            spaces = [s for s in (spaces or extra) if s in extra]
        m = re.search(r'text ~ "((?:[^"\\]|\\.)*)"', cql)
        words = (m.group(1).replace('\\"', '"') if m else "").lower().split()
        hits = []
        for p in _all_pages():
            if spaces is not None and p["space"]["key"] not in spaces:
                continue
            hay = (p["title"] + " " + p["body"]["storage"]["value"]).lower()
            score = sum(1 for w in words if w in hay)
            if words and score:
                q = dict(p); q["excerpt"] = re.sub("<[^>]+>", "", p["body"]["storage"]["value"])[:200]
                hits.append((score, q))
        hits = [q for _, q in sorted(hits, key=lambda x: -x[0])]
        return {"results": hits[:limit], "size": min(len(hits), limit)}

    @app.get("/rest/api/search")
    def search_api(cql: str, start: int = 0, limit: int = 25, expand: str = ""):
        """Newer search API (used by mcp-atlassian): results wrap the content object and carry an excerpt."""
        hits = search(cql, limit=1000)["results"]
        page = hits[start:start + limit]
        return {"results": [{"content": {k: v for k, v in h.items() if k != "excerpt"}, "excerpt": h["excerpt"],
                             "title": h["title"], "url": h["_links"]["webui"], "entityType": "content"} for h in page],
                "start": start, "limit": limit, "size": len(page), "totalSize": len(hits), "cqlQuery": cql}

    @app.get("/rest/api/content/{page_id}")
    def get_page(page_id: str, expand: str = ""):
        for p in _all_pages():
            if p["id"] == page_id:
                return p
        raise HTTPException(404, "page not found")

elif MODE == "jama":
    # Jama Connect REST v1 subset: GET /rest/v1/items?project=<id>&startAt=&maxResults=  and  GET /rest/v1/items/{id}
    def _items(project: int):
        for it in fix().get("projects", {}).get(project, []):
            yield {"id": it["id"], "documentKey": it.get("documentKey"), "itemType": it.get("itemType"),
                   "project": project, "modifiedDate": it.get("modifiedDate"), "fields": it.get("fields", {})}

    @app.get("/rest/v1/items")
    def items(project: int, startAt: int = 0, maxResults: int = 20):
        maxResults = min(maxResults, 50)                       # Jama caps page size at 50
        all_items = list(_items(project))
        page = all_items[startAt:startAt + maxResults]
        return {"meta": {"status": "OK", "pageInfo": {"startIndex": startAt, "resultCount": len(page), "totalResults": len(all_items)}},
                "links": {}, "data": page}

    @app.get("/rest/v1/items/{item_id}")
    def item(item_id: int):
        for project in fix().get("projects", {}):
            for it in _items(project):
                if it["id"] == item_id:
                    return {"meta": {"status": "OK"}, "data": it}
        raise HTTPException(404, "item not found")

else:
    @app.get("/api/catalog/entities")
    def entities(filter: str = Query(default="")):
        kind = filter.split("kind=", 1)[1] if "kind=" in filter else None
        return [e for e in fix().get("entities", []) if not kind or e["kind"].lower() == kind.lower()]

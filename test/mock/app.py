"""Tiny stand-ins for the two REST APIs the built-in adapters call.

MOCK=confluence : GET /rest/api/content?spaceKey=..&start=..&limit=..   (Confluence REST v1 shape)
MOCK=backstage  : GET /api/catalog/entities?filter=kind=..              (Backstage catalog shape)
Fixtures: /fixtures/confluence.yaml, /fixtures/backstage.yaml
"""
import os, re, yaml
from fastapi import FastAPI, HTTPException, Query

MODE = os.environ.get("MOCK", "confluence")
FIX = yaml.safe_load(open(f"/fixtures/{MODE}.yaml"))
app = FastAPI(title=f"mock-{MODE}")


@app.get("/health")
def health():
    return {"ok": True, "mock": MODE}


if MODE == "confluence":
    @app.get("/rest/api/content")
    def content(spaceKey: str, start: int = 0, limit: int = 50, type: str = "page", status: str = "current",
                expand: str = ""):
        pages = FIX.get("spaces", {}).get(spaceKey, [])
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
        for sk in FIX.get("spaces", {}):
            for p in content(sk, 0, 10000)["results"]:
                p["space"] = {"key": sk}
                yield p

    @app.get("/rest/api/content/search")
    def search(cql: str, limit: int = 25, expand: str = ""):
        # understands:  type=page AND space in ("A","B") AND text ~ "words"
        spaces = re.findall(r'"([^"]+)"', cql.split("space in", 1)[1].split(")", 1)[0]) if "space in" in cql else None
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

    @app.get("/rest/api/content/{page_id}")
    def get_page(page_id: str, expand: str = ""):
        for p in _all_pages():
            if p["id"] == page_id:
                return p
        raise HTTPException(404, "page not found")

else:
    @app.get("/api/catalog/entities")
    def entities(filter: str = Query(default="")):
        kind = filter.split("kind=", 1)[1] if "kind=" in filter else None
        return [e for e in FIX.get("entities", []) if not kind or e["kind"].lower() == kind.lower()]

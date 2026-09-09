"""Tiny stand-ins for the two REST APIs the built-in adapters call.

MOCK=confluence : GET /rest/api/content?spaceKey=..&start=..&limit=..   (Confluence REST v1 shape)
MOCK=backstage  : GET /api/catalog/entities?filter=kind=..              (Backstage catalog shape)
Fixtures: /fixtures/confluence.yaml, /fixtures/backstage.yaml
"""
import os, yaml
from fastapi import FastAPI, Query

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

else:
    @app.get("/api/catalog/entities")
    def entities(filter: str = Query(default="")):
        kind = filter.split("kind=", 1)[1] if "kind=" in filter else None
        return [e for e in FIX.get("entities", []) if not kind or e["kind"].lower() == kind.lower()]

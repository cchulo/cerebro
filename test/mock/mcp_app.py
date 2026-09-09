"""Mock Confluence MCP upstream (tools shaped like sooperset/mcp-atlassian) over streamable HTTP, backed by the same
fixtures as the REST mock. Used to test the gateway's `via: mcp` live source path.
  confluence_search(query=<CQL>, limit)  -> JSON list of pages {id, title, space{key}, url, excerpt}
  confluence_get_page(page_id)           -> JSON {metadata: {id, title, space{key}, url, version}, content: {value: markdown}}
"""
import json, os, re, yaml
from mcp.server.fastmcp import FastMCP

FIX = yaml.safe_load(open("/fixtures/confluence.yaml"))
mcp = FastMCP("mock-confluence-mcp", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
              streamable_http_path="/mcp", stateless_http=True, json_response=True)


def _pages():
    for sk, pages in FIX.get("spaces", {}).items():
        for p in pages:
            yield sk, p


def _strip(html: str) -> str:
    return re.sub("<[^>]+>", "", html)


@mcp.tool()
def confluence_search(query: str, limit: int = 10) -> str:
    """Search Confluence with CQL."""
    spaces = re.findall(r'"([^"]+)"', query.split("space in", 1)[1].split(")", 1)[0]) if "space in" in query else None
    m = re.search(r'text ~ "((?:[^"\\]|\\.)*)"', query)
    words = (m.group(1).replace('\\"', '"') if m else "").lower().split()
    hits = []
    for sk, p in _pages():
        if spaces is not None and sk not in spaces:
            continue
        hay = (p["title"] + " " + p["body"]).lower()
        score = sum(1 for w in words if w in hay)
        if words and score:
            # NOTE: deliberately no restriction metadata, like most MCP upstreams
            hits.append((score, {"id": str(p["id"]), "title": p["title"], "space": {"key": sk},
                                 "url": f"https://wiki.example/spaces/{sk}/pages/{p['id']}", "excerpt": _strip(p["body"])[:200]}))
    return json.dumps([h for _, h in sorted(hits, key=lambda x: -x[0])][:limit])


@mcp.tool()
def confluence_get_page(page_id: str) -> str:
    """Get a Confluence page by id."""
    for sk, p in _pages():
        if str(p["id"]) == page_id:
            return json.dumps({"metadata": {"id": page_id, "title": p["title"], "space": {"key": sk},
                                            "url": f"https://wiki.example/spaces/{sk}/pages/{page_id}",
                                            "version": p.get("version", 1)},
                               "content": {"value": _strip(p["body"]), "format": "markdown"}})
    raise ValueError(f"page {page_id} not found")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")

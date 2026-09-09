"""Identity-aware MCP gateway: one endpoint per developer, scope enforcement server-side.

Tools exposed to agents:
  list_scopes()                                  what the caller may see
  query_docs(query, mode, scopes?)               fan-out to the caller's LightRAG scope instances
  search_code(query, max_results)                Sourcebot search restricted to the caller's repos
  code_graph(scope, tool, arguments)             proxy a CodeGraphContext tool inside one allowed scope
  recall(query, bank?) / retain(content, bank?) / reflect(query, bank?)
                                                 Hindsight, limited to the caller's personal + team banks
"""
import asyncio, json, os, re
import httpx
from mcp.server.fastmcp import FastMCP, Context
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from . import acl

HINDSIGHT = os.environ.get("HINDSIGHT_URL", "http://hindsight:8888")
SOURCEBOT = os.environ.get("SOURCEBOT_URL", "http://sourcebot:3000")
SOURCEBOT_KEY = os.environ.get("SOURCEBOT_API_KEY", "")
LIGHTRAG_KEY = os.environ.get("LIGHTRAG_API_KEY", "")
PORT = int(os.environ.get("PORT", "8090"))

mcp = FastMCP("agent-context-gateway", host="0.0.0.0", port=PORT, streamable_http_path="/mcp",
              stateless_http=True)

def _caller(ctx: Context) -> acl.Caller:
    return acl.caller_from_headers(ctx.request_context.request.headers)

def _lightrag_url(scope: str) -> str:
    return f"http://lightrag-{scope}:9621"

def _codegraph_url(scope: str) -> str:
    return f"http://codegraph-{scope}:8045/mcp"

# ----------------------------------------------------------------------------- scopes
@mcp.tool()
def list_scopes(ctx: Context) -> dict:
    """List the documentation/code scopes and memory banks the current user can access."""
    c = _caller(ctx)
    return {"user": c.user, "scopes": c.scopes, "repos": c.repos,
            "banks": [c.personal_bank] + c.team_banks}

# ----------------------------------------------------------------------------- docs
@mcp.tool()
async def query_docs(ctx: Context, query: str, mode: str = "hybrid", scopes: list[str] | None = None) -> dict:
    """Ask the documentation knowledge graphs (Confluence, Backstage, repo docs, ADRs).
    mode: local (specific facts) | global (themes across docs) | hybrid. Only scopes the user may read are queried."""
    c = _caller(ctx)
    targets = scopes or c.scopes
    for s in targets:
        acl.check_scope(c, s)
    if not targets:
        return {"answer": None, "note": "user has no document scopes"}
    headers = {"X-API-Key": LIGHTRAG_KEY} if LIGHTRAG_KEY else {}

    async def one(scope):
        async with httpx.AsyncClient(timeout=180, headers=headers) as h:
            r = await h.post(f"{_lightrag_url(scope)}/query",
                             json={"query": query, "mode": mode, "include_references": True})
            r.raise_for_status()
            body = r.json()
            return {"scope": scope, "answer": body.get("response"), "references": body.get("references", [])}

    results = await asyncio.gather(*(one(s) for s in targets), return_exceptions=True)
    return {"results": [r if not isinstance(r, Exception) else {"scope": s, "error": str(r)}
                        for s, r in zip(targets, results)]}

# ----------------------------------------------------------------------------- code search
def _repo_filter(repos: list[str]) -> str:
    # Sourcebot/Zoekt query syntax: repo:<regex>. Build an anchored alternation of owner/name.
    names = []
    for url in repos:
        m = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
        if m:
            names.append(re.escape(m.group(1)))
    return "(" + "|".join(f"repo:^{n}$" for n in names) + ")" if names else "repo:^$"  # match nothing if no repos

@mcp.tool()
async def search_code(ctx: Context, query: str, max_results: int = 20) -> dict:
    """Exact / regex / symbol search across the repositories the user can read (Sourcebot).
    Examples: 'handlePayment', 'lang:go func New', 'file:Dockerfile FROM'."""
    c = _caller(ctx)
    scoped_query = f"{query} {_repo_filter(c.repos)}"
    headers = {"Content-Type": "application/json", "X-Org-Domain": "~"}
    if SOURCEBOT_KEY:
        headers["Authorization"] = f"Bearer {SOURCEBOT_KEY}"
    async with httpx.AsyncClient(timeout=60, headers=headers) as h:
        r = await h.post(f"{SOURCEBOT}/api/search", json={"query": scoped_query, "matches": max_results})
        r.raise_for_status()
        data = r.json()
    # belt and braces: drop anything outside the allowlist even if the filter was ignored
    allowed = {re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", u).group(1).lower() for u in c.repos}
    files = [f for f in data.get("files", []) if any(f.get("repository", "").lower().endswith(a) for a in allowed)]
    return {"query": scoped_query, "files": files[:max_results]}

# ----------------------------------------------------------------------------- code graph
@mcp.tool()
async def code_graph(ctx: Context, scope: str, tool: str, arguments: dict | None = None) -> dict:
    """Call a CodeGraphContext tool (e.g. find_code, analyze_code_relationships, list_indexed_repositories,
    find_dead_code, execute_cypher_query) inside ONE scope the user can read. Use list_scopes first."""
    c = _caller(ctx)
    acl.check_scope(c, scope)
    async with streamablehttp_client(_codegraph_url(scope)) as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()
            res = await s.call_tool(tool, arguments or {})
            return {"scope": scope, "content": [getattr(b, "text", str(b)) for b in res.content]}

# ----------------------------------------------------------------------------- memory
def _bank(c: acl.Caller, bank: str | None) -> str:
    allowed = [c.personal_bank] + c.team_banks
    b = bank or c.personal_bank
    if b not in allowed:
        raise PermissionError(f"bank '{b}' not allowed; use one of {allowed}")
    return b

async def _hs(method: str, path: str, **kw):
    async with httpx.AsyncClient(timeout=120, base_url=HINDSIGHT) as h:
        r = await h.request(method, path, **kw)
        r.raise_for_status()
        return r.json()

@mcp.tool()
async def recall(ctx: Context, query: str, bank: str | None = None, max_results: int = 10) -> dict:
    """Recall relevant memories (prior work, decisions, corrections). Defaults to the user's personal bank;
    pass a team bank (see list_scopes) for shared memory."""
    c = _caller(ctx); b = _bank(c, bank)
    return await _hs("POST", f"/v1/default/banks/{b}/memories/recall", json={"query": query, "max_results": max_results})

@mcp.tool()
async def retain(ctx: Context, content: str, bank: str | None = None, context: str | None = None) -> dict:
    """Store what happened: outcomes, decisions, things the docs got wrong. Never store restricted-doc content
    in a team bank the whole team can read."""
    c = _caller(ctx); b = _bank(c, bank)
    payload = {"items": [{"content": content, **({"context": context} if context else {})}]}
    return await _hs("POST", f"/v1/default/banks/{b}/memories", json=payload)

@mcp.tool()
async def reflect(ctx: Context, query: str, bank: str | None = None) -> dict:
    """Ask the memory bank to reason over everything it knows about a question."""
    c = _caller(ctx); b = _bank(c, bank)
    return await _hs("POST", f"/v1/default/banks/{b}/reflect", json={"query": query})

if __name__ == "__main__":
    mcp.run(transport="streamable-http")

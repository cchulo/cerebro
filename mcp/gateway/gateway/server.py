"""Identity-aware MCP gateway: one endpoint per developer, scope enforcement server-side.

Tools exposed to agents:
  list_scopes()                                  what the caller may see
  query_docs(query, mode, scopes?)               fan-out to the caller's LightRAG scope instances
  search_code(query, max_results, regex)         Sourcebot search restricted to the caller's repos
  code_graph(scope, tool, arguments)             proxy a read-only CodeGraphContext tool inside one allowed scope
  recall(query, bank?) / retain(content, bank?) / reflect(query, bank?)
                                                 Hindsight, limited to the caller's personal + team banks

Engine APIs verified against: LightRAG v1.5.7, Sourcebot v5.1.10, Hindsight 0.9.2, CodeGraphContext 0.6.13.
"""
import asyncio, os, re
import httpx
from mcp.server.fastmcp import FastMCP, Context
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from . import acl

HINDSIGHT = os.environ.get("HINDSIGHT_URL", "http://hindsight:8888")
HINDSIGHT_KEY = os.environ.get("HINDSIGHT_API_KEY", "")      # HINDSIGHT_API_TENANT_API_KEY on the engine side
SOURCEBOT = os.environ.get("SOURCEBOT_URL", "http://sourcebot:3000")
SOURCEBOT_KEY = os.environ.get("SOURCEBOT_API_KEY", "")
LIGHTRAG_KEY = os.environ.get("LIGHTRAG_API_KEY", "")
PORT = int(os.environ.get("PORT", "8090"))

# Stateless + JSON responses: every request carries the proxy's identity headers, nothing is kept per session.
mcp = FastMCP("agent-context-gateway", host="0.0.0.0", port=PORT, streamable_http_path="/mcp",
              stateless_http=True, json_response=True)

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
DOC_MODES = ("local", "global", "hybrid", "mix", "naive")

def _decode_source(file_path: str) -> str:
    # ingest encodes "/" as "|" because LightRAG keeps only the basename of a source id (see ingest/ingest/lightrag.py)
    return file_path.replace("|", "/")

@mcp.tool()
async def query_docs(ctx: Context, query: str, mode: str = "mix", scopes: list[str] | None = None) -> dict:
    """Ask the documentation knowledge graphs (Confluence, Backstage, repo docs, ADRs).
    mode: local (specific facts) | global (themes across docs) | hybrid | mix (graph + vector, default) | naive (vector only).
    Only scopes the user may read are queried; omit `scopes` to query all of them."""
    c = _caller(ctx)
    if mode not in DOC_MODES:
        raise ValueError(f"mode must be one of {DOC_MODES}")
    targets = scopes or c.scopes
    for s in targets:
        acl.check_scope(c, s)
    if not targets:
        return {"results": [], "note": "user has no document scopes"}
    headers = {"X-API-Key": LIGHTRAG_KEY} if LIGHTRAG_KEY else {}

    async def one(scope):
        async with httpx.AsyncClient(timeout=300, headers=headers) as h:
            r = await h.post(f"{_lightrag_url(scope)}/query",
                             json={"query": query, "mode": mode, "include_references": True})
            r.raise_for_status()
            body = r.json()
            return {"scope": scope, "answer": body.get("response"),
                    "references": [{"id": ref.get("reference_id"), "source": _decode_source(ref.get("file_path", ""))}
                                   for ref in body.get("references") or []]}

    results = await asyncio.gather(*(one(s) for s in targets), return_exceptions=True)
    return {"results": [r if not isinstance(r, Exception) else {"scope": s, "error": str(r)}
                        for s, r in zip(targets, results)]}

# ----------------------------------------------------------------------------- code search
def _repo_name(url: str) -> str | None:
    """https://github.com/org/x.git | git@github.com:org/x.git -> github.com/org/x (Sourcebot's repo name form)."""
    m = re.match(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^/:]+)[:/](.+?)(?:\.git)?/?$", url.strip())
    return f"{m.group(1)}/{m.group(2)}".lower() if m else None

def _repo_filter(repos: list[str]) -> str:
    # Sourcebot query language (v5): `repo:` values are always regexes, alternation is the `or` keyword.
    names = [n for n in (_repo_name(u) for u in repos) if n]
    if not names:
        return "repo:^$"          # matches nothing
    return "(" + " or ".join(f"repo:^{re.escape(n)}$" for n in names) + ")"

@mcp.tool()
async def search_code(ctx: Context, query: str, max_results: int = 20, regex: bool = False) -> dict:
    """Search source code across the repositories the user can read (Sourcebot / Zoekt).
    Query syntax: bare terms are AND'ed, filters: file:<regex> lang:<name> sym:<symbol> rev:<branch>, negate with -,
    group with ( ) and `or`. Set regex=true to treat bare terms as regular expressions."""
    c = _caller(ctx)
    scoped_query = f"({query}) {_repo_filter(c.repos)}"
    headers = {"Content-Type": "application/json"}
    if SOURCEBOT_KEY:
        headers["Authorization"] = f"Bearer {SOURCEBOT_KEY}"
    async with httpx.AsyncClient(timeout=60, headers=headers) as h:
        r = await h.post(f"{SOURCEBOT}/api/search",
                         json={"query": scoped_query, "matches": max_results, "contextLines": 2,
                               "isRegexEnabled": regex})
        r.raise_for_status()
        data = r.json()
    # belt and braces: drop anything outside the allowlist even if the filter was ignored
    allowed = {n for n in (_repo_name(u) for u in c.repos) if n}
    files = []
    for f in data.get("files", []):
        if f.get("repository", "").lower() not in allowed:
            continue
        files.append({"repository": f["repository"], "file": f["fileName"]["text"], "language": f.get("language"),
                      "url": f.get("webUrl"),
                      "chunks": [{"line": ch["contentStart"]["lineNumber"], "content": ch["content"]}
                                 for ch in f.get("chunks", [])]})
    return {"query": scoped_query, "total_matches": data.get("stats", {}).get("totalMatchCount"),
            "files": files[:max_results]}

# ----------------------------------------------------------------------------- code graph
# Only read-only CodeGraphContext tools are proxied. Indexing, watching, deleting, bundle loading and context
# switching are server-side operations (indexer-<scope> job), never available to agents.
CODEGRAPH_TOOLS = {
    "find_code", "analyze_code_relationships", "list_indexed_repositories", "list_graphs", "get_repository_stats",
    "find_dead_code", "find_most_complex_functions", "calculate_cyclomatic_complexity", "execute_cypher_query",
    "simulate_metrics", "analyze_architectural_evolution", "generate_report", "find_java_spring_beans",
    "find_java_spring_endpoints", "find_datasource_nodes", "check_job_status", "list_jobs",
}

@mcp.tool()
async def code_graph(ctx: Context, scope: str, tool: str, arguments: dict | None = None) -> dict:
    """Call a read-only CodeGraphContext tool inside ONE scope the user can read. Use list_scopes first.
    Tools: find_code, analyze_code_relationships, list_indexed_repositories, list_graphs, find_dead_code,
    find_most_complex_functions, calculate_cyclomatic_complexity, execute_cypher_query, get_repository_stats, ..."""
    c = _caller(ctx)
    acl.check_scope(c, scope)
    if tool not in CODEGRAPH_TOOLS:
        raise PermissionError(f"tool '{tool}' is not allowed through the gateway; allowed: {sorted(CODEGRAPH_TOOLS)}")
    async with streamablehttp_client(_codegraph_url(scope)) as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()
            res = await s.call_tool(tool, arguments or {})
            return {"scope": scope, "is_error": bool(res.isError),
                    "content": [getattr(b, "text", str(b)) for b in res.content]}

# ----------------------------------------------------------------------------- memory
BUDGETS = ("low", "mid", "high")

def _bank(c: acl.Caller, bank: str | None) -> str:
    allowed = [c.personal_bank] + c.team_banks
    b = bank or c.personal_bank
    if b not in allowed:
        raise PermissionError(f"bank '{b}' not allowed; use one of {allowed}")
    return b

async def _hs(method: str, path: str, **kw):
    headers = {"Authorization": f"Bearer {HINDSIGHT_KEY}"} if HINDSIGHT_KEY else {}
    async with httpx.AsyncClient(timeout=300, base_url=HINDSIGHT, headers=headers) as h:
        r = await h.request(method, path, **kw)
        if r.status_code == 404:          # bank does not exist yet: nothing has been retained
            return None
        r.raise_for_status()
        return r.json()

@mcp.tool()
async def recall(ctx: Context, query: str, bank: str | None = None, budget: str = "mid", max_tokens: int = 4096) -> dict:
    """Recall relevant memories (prior work, decisions, corrections). Defaults to the user's personal bank;
    pass a team bank (see list_scopes) for shared memory. budget: low | mid | high."""
    c = _caller(ctx); b = _bank(c, bank)
    if budget not in BUDGETS:
        raise ValueError(f"budget must be one of {BUDGETS}")
    res = await _hs("POST", f"/v1/default/banks/{b}/memories/recall",
                    json={"query": query, "budget": budget, "max_tokens": max_tokens})
    return res if res is not None else {"bank": b, "results": [], "note": "bank is empty"}

@mcp.tool()
async def retain(ctx: Context, content: str, bank: str | None = None, context: str | None = None,
                 tags: list[str] | None = None) -> dict:
    """Store what happened: outcomes, decisions, things the docs got wrong. Never store restricted-doc content
    in a team bank the whole team can read."""
    c = _caller(ctx); b = _bank(c, bank)
    item = {"content": content}
    if context:
        item["context"] = context
    if tags:
        item["tags"] = tags
    # async: Hindsight queues extraction (LLM) and returns an operation id; the agent does not wait for the model
    res = await _hs("POST", f"/v1/default/banks/{b}/memories", json={"items": [item], "async": True})
    return {"bank": b, **(res or {})}

@mcp.tool()
async def reflect(ctx: Context, query: str, bank: str | None = None, budget: str = "low",
                  context: str | None = None) -> dict:
    """Ask the memory bank to reason over everything it knows about a question. budget: low | mid | high."""
    c = _caller(ctx); b = _bank(c, bank)
    if budget not in BUDGETS:
        raise ValueError(f"budget must be one of {BUDGETS}")
    payload = {"query": query, "budget": budget}
    if context:
        payload["context"] = context
    res = await _hs("POST", f"/v1/default/banks/{b}/reflect", json=payload)
    return res if res is not None else {"bank": b, "text": None, "note": "bank is empty"}

if __name__ == "__main__":
    mcp.run(transport="streamable-http")

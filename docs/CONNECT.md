# Connecting Claude Code, Cursor and other MCP clients

Every developer connects to **one** URL: the gateway behind SSO. It exposes the docs, code and memory tools
and enforces what the caller may see (see ACCESS-CONTROL.md). Engines are not exposed to clients.

```
https://context.internal/mcp
```

Login happens once in the browser via the SSO proxy; MCP clients that support OAuth/streamable HTTP will
open it automatically, others need the proxy's cookie or a bearer token from your IdP.

## Claude Code

```sh
claude mcp add --transport http context https://context.internal/mcp --scope user
claude mcp list
```

`--scope project` instead writes `.mcp.json` into the repo so the whole team inherits it.

## Cursor (`~/.cursor/mcp.json`)

```json
{ "mcpServers": { "context": { "url": "https://context.internal/mcp" } } }
```

Claude Desktop, Windsurf, VS Code/Copilot, Codex CLI and Gemini CLI take the same URL.

## Tools the agent sees

| Tool | What it does |
|---|---|
| `list_scopes` | which scopes, repos and memory banks this user can use |
| `query_docs(query, mode, scopes?)` | Confluence / Backstage / repo docs across the user's scopes (mode mix/local/global/hybrid/naive) |
| `search_code(query, max_results, regex)` | Zoekt search (`file:`, `lang:`, `sym:`, `-`, `or`), restricted to the user's repos |
| `code_graph(scope, tool, arguments)` | read-only CodeGraphContext tools (`find_code`, `analyze_code_relationships`, `execute_cypher_query`, ...) inside one scope |
| `recall / retain / reflect` | Hindsight, personal bank by default, team banks by group (`budget` low/mid/high) |

## Routing rule for `CLAUDE.md` / `.cursor/rules`

```
Use the `context` MCP server:
- query_docs: what our documentation, ADRs, runbooks and Backstage catalog say ("how do we", "who owns", "policy").
- search_code: find exact code, symbols, file paths across repos.
- code_graph: callers/callees, blast radius, dead code — structural questions. Call list_scopes first to pick the scope.
- recall at the start of a task for prior context; retain at the end with outcomes, decisions and anything the docs got wrong.
Never retain content from restricted documents into a team bank.
```

## Verify

`make smoke` runs the access-control checks against the gateway directly (forged identity headers, no proxy).
Through the proxy: log in as a user in no extra group and as a user in `payments-team`; run `list_scopes` for each and
confirm the difference. Then: `query_docs` "deploy runbook", `search_code` "TODO",
`code_graph public find_code {"query": "main"}`, `recall` "what did we do last week".

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

## Without a proxy (laptop, demo)

The gateway reads the identity from `X-Forwarded-User` / `X-Forwarded-Groups`. Where no SSO proxy sits in front
(the compose stack bound to 127.0.0.1, the demo), the client sends those headers itself:

```sh
claude mcp add --transport http context http://127.0.0.1:8090/mcp --scope user \
  --header "X-Forwarded-User: alice" --header "X-Forwarded-Groups: payments-team"
```

```json
{ "mcpServers": { "context": { "url": "http://127.0.0.1:8090/mcp",
    "headers": { "X-Forwarded-User": "alice", "X-Forwarded-Groups": "payments-team" } } } }
```

`scripts/demo.sh connect --as <persona>` prints these for the demo users, `--write` places them in the repository
(`.mcp.json`, `.cursor/mcp.json`). Anyone who can reach the port can claim any identity this way, which is why the
gateway must never be exposed without the proxy ([ACCESS-CONTROL.md](ACCESS-CONTROL.md)).

## Tools the agent sees

| Tool | What it does |
|---|---|
| `list_scopes` | which scopes, repos and memory banks this user can use |
| `query_docs(query, mode, scopes?)` | Confluence / Backstage / repo docs across the user's scopes (mode mix/local/global/hybrid/naive) |
| `live_search(source, query, scopes?)` / `live_fetch(source, ref)` | the system of record directly through the plugin's live part (Confluence via mcp-atlassian), when the index missed or may lag; same scopes, restricted pages never served; `query_docs` falls back to them automatically |
| `search_code(query, max_results, regex)` | Zoekt search (`file:`, `lang:`, `sym:`, `rev:` for indexed branches, `-`, `or`), restricted to the user's repos |
| `code_graph(scope, tool, arguments)` | read-only CodeGraphContext tools (`find_code`, `analyze_code_relationships`, `execute_cypher_query`, ...) inside one scope |
| `recall / retain / reflect` | Hindsight, personal bank by default, team banks by group (`budget` low/mid/high) |

## How agents know when to recall and retain

**Nothing is installed on the agent.** An MCP client learns everything from the server at connect time, in three
layers, from softest to hardest:

| Layer | Mechanism | What it guarantees |
|---|---|---|
| 1. Server instructions | The gateway returns an `instructions` text in the MCP `initialize` response (`INSTRUCTIONS` in `mcp/gateway/gateway/server.py`). Claude Code, Cursor and most clients put it in the model's context. It carries the routing (docs / code / graph) and the rule "recall at the start of a task, retain at the end". | The model has read the rule. It usually follows it. |
| 2. Tool descriptions | Every tool's docstring says when to use it; `retain`'s says what to store and what never to store. | Same as above, at the moment of choosing a tool. |
| 3. Prompts as commands | The gateway exposes two MCP prompts. Claude Code lists them as slash commands: `/mcp__context__start_task <task>` (recall, then look things up) and `/mcp__context__wrap_up` (retain the outcome). Other clients show them in their prompt picker. | Deterministic when a person runs it. |

The honest limit: with layers 1–3 alone a model can still finish a task without calling `retain`. If you need it
every time, make the **client** enforce it. Claude Code example, a `Stop` hook that refuses to end the session until
the agent has retained (team `.claude/settings.json`):

```json
{
  "hooks": {
    "Stop": [{ "hooks": [{ "type": "command", "command": "python3 .claude/hooks/require-retain.py" }] }]
  }
}
```

```python
#!/usr/bin/env python3
# .claude/hooks/require-retain.py — block the first stop of a session until `retain` has been called.
import json, sys
event = json.load(sys.stdin)
if event.get("stop_hook_active"):          # we already blocked once; let it stop now
    sys.exit(0)
transcript = open(event["transcript_path"]).read()
if "mcp__context__retain" in transcript:  # the tool was used at least once this session
    sys.exit(0)
print(json.dumps({"decision": "block",
                  "reason": "Before finishing: call the context server's `retain` with one or two sentences on what "
                            "changed, decisions made, and anything the docs got wrong (see /mcp__context__wrap_up)."}))
```

Cursor and other clients have their own hook/rules mechanisms; the routing rule below is the portable fallback.
The name `context` in the slash commands is whatever you called the server when adding it (`claude mcp add ... context`).

## Routing rule for `CLAUDE.md` / `.cursor/rules`

```
Use the `context` MCP server:
- query_docs: what our documentation, ADRs, runbooks and Backstage catalog say ("how do we", "who owns", "policy").
  If it returns `fallback` hits, the index had no answer: live_fetch the relevant ref.
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

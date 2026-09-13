# Connecting Claude Code, Cursor and other MCP clients

Every developer connects to **one** URL: the gateway's `gateway.public_url` (or `http://<host>:8090/mcp`). It
exposes the docs, code and memory tools and enforces what the caller may see ([ACCESS-CONTROL.md](ACCESS-CONTROL.md)).
Engines are never exposed to clients. What the client has to present depends on `identity.mode`
([IDENTITY.md](IDENTITY.md)).

## Mode `none` (one machine)

Loopback, no token. The gateway must run on the host for this (`cerebro gateway serve`, which binds 127.0.0.1):

```sh
claude mcp add --transport http cerebro http://127.0.0.1:8090/mcp --scope user
```

With the stack in compose, the gateway runs in a container and needs `identity.allow_remote: true` plus the static
token (README quick start). Then every client sends it as a header:

```sh
claude mcp add --transport http cerebro http://127.0.0.1:8090/mcp --scope user \
  --header "Authorization: Bearer $CEREBRO_TOKEN"
```

```json
{ "mcpServers": { "cerebro": { "url": "http://127.0.0.1:8090/mcp",
    "headers": { "Authorization": "Bearer <CEREBRO_TOKEN>" } } } }
```

(`~/.cursor/mcp.json`; `.cursor/mcp.json` in a repository for the project.) Verified: a request without the header is
401, with it the MCP `initialize` succeeds.

## Modes `builtin` and `external` (OAuth)

Give the client the URL and nothing else:

```sh
claude mcp add --transport http cerebro https://context.example.org/mcp --scope user
claude mcp list
```

```json
{ "mcpServers": { "cerebro": { "url": "https://context.example.org/mcp" } } }
```

What happens next, and what to expect:

1. The client's first request gets `401` with a `WWW-Authenticate` header pointing at
   `https://context.example.org/.well-known/oauth-protected-resource`.
2. The client reads it (`authorization_servers: [<issuer>]`, `scopes_supported: [cerebro:docs.read, ...]`), reads the
   issuer's discovery document, and registers itself: with Keycloak `builtin`, anonymous Dynamic Client Registration
   is allowed for loopback redirect URIs, which is what Claude Code and Cursor use; a client that supports Client ID
   Metadata Documents can use those instead when the realm has the `cimd` feature on. A pre-registered public client
   `cerebro-mcp` exists for clients that ask for a client id by hand.
3. A browser window opens on the authorization server. In `builtin` mode a seeded user logs in with the temporary
   password and registers a passkey (or sets a password); in `external` mode it is your IdP's login.
4. The client receives an access token whose `aud` is the gateway (audience mapper in `builtin`; your IdP's
   configuration in `external`) and retries. `tools/list` now shows the tools the token's scopes allow; `whoami`
   shows subject, groups, scopes, repositories and banks.

Claude Code and Cursor run this flow themselves for streamable-HTTP servers; Claude Desktop, Windsurf, VS Code and
Codex CLI take the same URL. Tokens expire on the issuer's schedule; the client refreshes them.

Headless and service use (CI, a bot): obtain a token from the issuer with the client-credentials grant for a
confidential client your IdP (or the Keycloak admin) created with the `cerebro:*` scopes it needs, and send it as
`Authorization: Bearer ...` exactly like the static token above. It becomes a service principal: scopes from its
groups, no personal memory bank, no `retain` unless it carries `cerebro:memory.write`.

## Tools the agent sees

| Tool | Scope | What it does |
|---|---|---|
| `whoami` | any | subject, kind, groups, token scopes, scopes, repositories with tracked branches, banks |
| `list_scopes` | any | scopes, repository URLs and memory banks of the caller |
| `query_docs(query, mode?, scopes?, fallback)` | docs.read | the document indexes of the caller's scopes; `mode` is engine specific (LightRAG: `local`, `global`, `hybrid`, `mix` (default), `naive`); on a miss with `fallback` the live sources are searched and their hits returned under `fallback` |
| `live_search(source, query, scopes?, limit)` / `live_fetch(source, ref, max_chars)` | docs.read | a system of record directly (`confluence`), confined to the caller's spaces; restricted pages are never served |
| `search_code(query, scopes?, branch?, regex, max_results)` | code.read | text search across every code unit that serves one of the caller's repositories; `regex` for a regular expression; `branch` for a tracked branch |
| `list_code_units()` | code.read | the units the caller may query, each with its engine capabilities and the read-only tools it exposes |
| `code_tool(unit, tool, arguments?, branch?)` | code.read | one read-only engine tool on one unit (`tokensave_callers`, `tokensave_search`, ...); `arguments.repo` picks a repository in a multi-repo unit |
| `recall(query, bank?, budget, max_tokens)` | memory.read | memories from the personal bank or a team bank (`budget`: `low`, `mid`, `high`) |
| `retain(content, bank?, context?, tags?)` | memory.write | store an outcome; extraction runs asynchronously |
| `reflect(query, bank?, budget, context?)` | memory.read | let the memory engine reason over a bank (hidden when the engine cannot) |

## How agents know when to recall and retain

Nothing is installed on the agent. The client learns everything from the server at connect time, softest to hardest:

| Layer | Mechanism | What it guarantees |
|---|---|---|
| 1. Server instructions | the gateway's MCP `instructions` (`INSTRUCTIONS` in `cerebro/gateway/server.py`): routing between `query_docs`, `search_code`, `code_tool`, `live_search`, and the rule "recall at the start of a task, retain at the end; never retain restricted content into a team bank" | the model has read the rule; it usually follows it |
| 2. Tool descriptions | every tool's docstring says when to use it; `retain`'s says what never to store | the same, at the moment of choosing a tool |
| 3. Prompts as commands | two MCP prompts: `start_task(task)` (recall, then `query_docs` / `search_code` / `code_tool`, then summarise prior decisions) and `wrap_up()` (one `retain` with what changed, decisions, what the docs got wrong). Claude Code lists them as `/mcp__cerebro__start_task` and `/mcp__cerebro__wrap_up`; other clients show them in their prompt picker | deterministic when a person runs it |

The honest limit: with these three alone a model can still finish without calling `retain`. If it must happen every
time, make the client enforce it, for example a Claude Code `Stop` hook that blocks the first stop of a session until
the transcript contains `mcp__cerebro__retain` (the v1 branch carries a worked example in `docs/CONNECT.md`). The
server name in the slash commands is whatever you passed to `claude mcp add`.

## Routing rule for `CLAUDE.md` / `.cursor/rules`

The server instructions already say this; keep a copy in the repository only if your client does not surface them:

```
Use the `cerebro` MCP server before guessing:
- query_docs for what our documentation, ADRs, runbooks and catalog say; if it returns `fallback` hits, live_fetch the ref.
- search_code for exact code, symbols and paths; list_code_units then code_tool for callers, blast radius, dead code.
- recall at the start of a task; retain at the end (what changed, decisions, anything the docs got wrong).
Never retain content from restricted documents into a team bank.
```

## Verify

Log in as a user in no extra group and as one in `payments-team`; run `list_scopes` for each and confirm the
difference. Then `query_docs("deploy runbook")`, `search_code("TODO")`, `list_code_units`, one `code_tool`, and
`recall("what did we do last week")`. A token without `cerebro:memory.write` must not list `retain`.

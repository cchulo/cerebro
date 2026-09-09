# Access control

## The problem

Graph RAG merges knowledge across documents at index time: entities and relationships from a restricted
Confluence page and a public one end up in the same node, and `global` mode answers from summaries of that
merged graph. Filtering returned chunks per user afterwards does **not** prevent leakage. The same is true of a
code graph: `analyze_code_relationships` walks edges regardless of which repo they came from.

So the access boundary has to be the **index**, not the query.

## The model: scopes

`config/scopes.yaml` defines **scopes**. A scope is a permission group with:

- `groups`: IdP groups allowed to read it (`everyone` = any authenticated user)
- `code.repos`: repositories indexed into it (code → Sourcebot + CodeGraphContext, docs inside them → the `git` plugin)
- `docs`: document sources indexed into it, one entry per plugin (`confluence: {spaces: [...]}`,
  `backstage: {}`, `git: {}`, `files: {paths: [...]}`, or any plugin in `plugins/`) → this scope's LightRAG

One scope therefore answers both questions, what documents and what code a group may see, and the gateway applies
it to every tool the same way.

Rules enforced by the generator and the ingest service:

1. A space or repo belongs to **exactly one** scope (the ingest refuses duplicates at startup). An adapter
   yields only what the whole scope may read; anything with a finer ACL (a restricted page) is skipped, never
   yielded — that is the adapter's responsibility, see `ingest/ingest/sources/base.py`.
2. Every scope gets its **own** LightRAG instance (docs graph) and its **own** FalkorDB + CodeGraphContext
   (code graph). Nothing is ever merged across scopes.
3. Confluence pages with **page-level read restrictions are skipped** (`restricted_pages: skip`). A page
   restriction means the space's scope is not a valid ACL for that page; if a formerly-public page becomes
   restricted, the next sync removes it.
4. Sourcebot is a single index, but the gateway appends `repo:` filters for the caller's repos to every query
   and additionally drops any result outside the allowlist.
5. Hindsight: `user-<id>` banks are private; `team-<group>` banks are shared with that IdP group only. Hindsight
   itself requires an API key on every call (`HINDSIGHT_API_KEY`) that only the gateway holds.
6. Live sources (`live_search`, `live_fetch`, and the automatic fallback in `query_docs`) are confined by the gateway
   to the caller's scopes using the same `docs:` entries: it builds the upstream query itself and re-checks every
   result's space, whether the transport is REST or an upstream MCP server (docs/SOURCES.md, "Fallbacks").
7. `code_graph` proxies a fixed allowlist of read-only CodeGraphContext tools; indexing, watching, deleting and
   bundle loading are only reachable from the server-side `indexer-<scope>` job.

## Identity

Agents never reach the engines directly. They talk to the **gateway** (`mcp/gateway`), which:

- reads `X-Forwarded-User` and `X-Forwarded-Groups` set by the SSO reverse proxy,
- computes the caller's scopes and banks,
- fans `query_docs` out to the caller's LightRAG instances, proxies `code_graph` calls into one allowed
  scope, filters `search_code`, and pins `recall`/`retain`/`reflect` to allowed banks.

The gateway trusts those headers because **only the proxy can reach port 8090**. Keep it on loopback or an
internal network (compose binds every port to 127.0.0.1; on Kubernetes every Service is ClusterIP and only the
SSO Ingress you add may route to `svc/gateway`). An example oauth2-proxy + Caddy config is in `config/proxy/`.

## What you give up

- Cross-scope questions are answered per scope and merged by the agent, not by one unified graph.
- Every scope is another LightRAG + FalkorDB pair (~1–2 GB RAM each). Keep scopes coarse: teams or
  business domains, not individual repos.
- Page-level Confluence restrictions are excluded, not modelled. If restricted pages are the norm rather
  than the exception in a space, that space is the wrong fit for a graph index; put it in a vector store with
  document-level security or leave it out.
- Group membership is read from the IdP at request time, so revoking access is immediate; moving a repo or
  space between scopes requires a re-index (`indexer-<scope>` + `POST /sync/all`).

## Checklist before opening to the team

- [ ] SSO proxy in front of the gateway; direct engine ports firewalled
- [ ] Every space/repo assigned to one scope; nothing "public" that isn't
- [x] Hindsight API auth enabled (`ApiKeyTenantExtension`, key in `config/stack.env`) so nobody can bypass the gateway
- [ ] Inference backend (`LLM_PROVIDER`/`EMBED_PROVIDER`) is local or an org-approved endpoint — see README
- [ ] Sourcebot reachable ONLY by the gateway: it runs with anonymous access (`FORCE_ENABLE_ANONYMOUS_ACCESS`) so
      the gateway needs no key; its port is loopback/ClusterIP and must never be exposed, or put its UI behind the
      same SSO and switch to `SOURCEBOT_API_KEY`
- [ ] `make smoke` passes: a user in *no* group sees only `public`, cannot query/search/recall anything else;
      a user in `payments-team` sees `public` + `payments` and the team bank

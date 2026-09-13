# Access control

## The problem

A graph index merges knowledge across its inputs at index time: entities and relations from a restricted page and a
public one end up in the same node, and a global-mode answer is written from summaries of that merged graph. A code
graph does the same with call edges across repositories. Filtering returned chunks per user afterwards does not undo
the merge. So the access boundary has to be the **index**, not the query. This argument is unchanged from v1.

## The model: scopes

`scopes:` in `cerebro.yaml` defines the permission groups. A scope has:

- `groups`: IdP groups allowed to read it (`everyone`, via `policy.always_groups`, means any authenticated caller)
- `code.repos`: repositories indexed into it, each with optional tracked `branches`
- `docs`: document sources indexed into it, one entry per plugin (`confluence: {spaces: [...]}`, `git: {}`,
  `files: {paths: [...]}`, `jama: {projects: [...]}`, `backstage: {}`)

One scope answers both what documents and what code a group may see, and every tool applies it the same way.

Rules enforced by the loader, the ingest engine and the gateway:

1. A repository or a Confluence space belongs to **exactly one** scope; `cerebro validate` refuses otherwise.
2. Every scope gets its own document index (`docs-<scope>`). Nothing is merged across scopes; a `Batch` names one
   scope and the LightRAG adapter refuses a batch for another.
3. A plugin yields only what the whole scope may read. Anything with a finer ACL (a Confluence page with a page-level
   read restriction) is skipped, never yielded; if it becomes restricted later the next sync removes it.
4. Live sources (`live_search`, `live_fetch`, the automatic fallback of `query_docs`) are confined by the gateway to
   the caller's scopes with the same `docs:` entries: it passes that list to the plugin as `allowed`, the plugin
   builds the upstream query from it and re-checks every result's space, whether it talks REST or an MCP upstream.
5. Memory banks are `user-<subject>` (private; service principals have none) and `team-<group>` (one per IdP group
   outside `always_groups`, when `policy.team_banks_from_groups` is on). Hindsight requires the API key on every call
   and only the gateway holds it.

## Units: what runs per scope

| Engine | Unit granularity | Isolation |
|---|---|---|
| docs (LightRAG) | always one server per scope, `docs-<scope>` | separate `WORKSPACE` per instance, one index each |
| code (TokenSave) | `engines.code.unit: scope` (default): `code-<scope>` with the scope's repositories checked out side by side; `repo`: `code-<scope>-<repo slug>`, one per repository; a scope may override with `code.unit` | one bridge process and one workspace volume per unit; graphs are per repository and per tracked branch (`.tokensave/` in each checkout); the bridge only addresses repositories of its own unit |
| memory (Hindsight) | one shared unit | partitioned by bank name, which only the policy chooses |

Branches: `branches:` on a repository lists what the index job tracks (globs resolved against origin at index time,
the default branch always). `search_code` and `code_tool` take `branch`; a branch that is not tracked is a tool error,
and an engine without branch support rejects the argument as unsupported.

`unit: repo` costs one workload per repository and gives you finer idle TTLs and volumes; `unit: scope` costs one
per scope and lets one call address several repositories at once. Either way a caller only ever reaches units whose
scope they hold.

## Identity modes and what each protects against

The gateway is always the resource server: it validates whatever proves identity and turns it into a `Principal`.
Who mints that proof is `identity.mode` ([IDENTITY.md](IDENTITY.md)).

| Mode | Identity comes from | Protects against | Does not protect against |
|---|---|---|---|
| `none` | the fixed `principal:`; loopback only | anything not on this machine | other processes on the machine; with `allow_remote` anyone without `CEREBRO_TOKEN` |
| `static` | a token map in the config | unknown tokens | leaked tokens (no expiry, no revocation); tests and demos only |
| `builtin` | Keycloak the stack runs; JWTs validated against its JWKS | forged or expired tokens, tokens for another audience, users outside the realm | compromise of the Keycloak admin credentials |
| `external` | your IdP; JWTs (JWKS) or opaque tokens (introspection) | the same, with your IdP's policies (MFA, lifetime, revocation via introspection) | claims your IdP does not send (groups must arrive in `groups_claim`) |
| legacy `trusted_headers` | `X-Forwarded-User` / `X-Forwarded-Groups` set by an SSO proxy | nothing by itself | anyone who can reach the gateway without the proxy: only safe when the proxy is the single route |

Revocation latency with JWTs is the token lifetime; use `token_validation: introspection` when it must be immediate.

## Token scopes per tool

A token carries OAuth scopes; the gateway hides tools the token cannot use from `tools/list` and refuses calls to
them. Mode `none` and plain OIDC tokens that carry no `cerebro:*` scope get every scope.

| Tool | Token scope |
|---|---|
| `whoami`, `list_scopes` | any authenticated caller |
| `query_docs`, `live_search`, `live_fetch` | `cerebro:docs.read` |
| `search_code`, `list_code_units`, `code_tool` | `cerebro:code.read` |
| `recall`, `reflect` | `cerebro:memory.read` |
| `retain` | `cerebro:memory.write` |
| everything | `cerebro:admin` implies all of the above |

Group membership still decides *which* scopes and banks; token scopes decide what the token may do with them. A CI
agent is a service principal with `cerebro:code.read` and `cerebro:docs.read`, no personal bank.

## What the gateway checks on every call

1. **Identity**: the middleware resolves the request or answers 401 with the provider's challenge. No principal, no tool.
2. **Token scope**: the tool's required scope against the token's, before anything else.
3. **Scope**: `query_docs`, `live_search`, `search_code` accept an optional `scopes` list and refuse any scope the
   caller does not hold (`Forbidden`); omitting it means all of the caller's scopes.
4. **Unit gating**: `code_tool` and `list_code_units` only see units of the caller's scopes; a unit outside them is
   `Forbidden`. `search_code` fans out only to units that serve one of the caller's repositories and asks each for
   the caller's repositories in it (`repos` narrows, never widens).
5. **Second-pass repository filter**: every hit returned by a code unit is dropped unless its `repository` is in the
   caller's repository list; the bridge applies the same rule inside the unit.
6. **Bank gating**: `recall`, `retain`, `reflect` default to the personal bank; a named bank must be in the caller's
   `banks` or the call is `Forbidden`.
7. **Read-only tools only**: the bridge exposes engine tools with `readOnlyHint` and a selector argument, never
   TokenSave's edit, session or memory tools; `code_tool` refuses names the unit does not expose, and the bridge strips
   any caller-supplied `graph_root` / `graph_branch` and injects its own.

## What you give up

- Cross-scope questions are answered per scope and merged by the agent, not by one unified graph.
- Every scope is another LightRAG server (about 1 GB) and at least one code unit. Keep scopes coarse: teams or
  domains, not individual repositories, unless you want `unit: repo`.
- Page-level Confluence restrictions are excluded, not modelled. A space where they are the norm belongs in a store
  with document-level security, not in a graph index.
- Moving a repository or space between scopes needs a re-index (`cerebro provision job index-<unit>`, then
  `cerebro ingest sync`); the old scope's index keeps the documents until the sync deletes them.
- Engines hold shared service credentials; the gateway is their only caller. Per-user tokens toward engines (RFC 8693)
  are not implemented.

## Checklist before opening to the team

- [ ] `identity.mode` is `builtin` or `external`; `none` and `static` are not in use
- [ ] `gateway.public_url` is the URL clients use (it is the token audience) and only a TLS proxy or Ingress reaches the gateway
- [ ] no engine port is published or routable from outside the stack network (compose publishes only the gateway; every k8s Service is ClusterIP)
- [ ] every space and repository is assigned to one scope; nothing in `public` that is not
- [ ] `secrets.env` values are generated, not the placeholders; `INGEST_WEBHOOK_SECRET` is set
- [ ] the inference backend is local or an endpoint your organisation approves (README, "What the model sees")
- [ ] the compose `scheduler` (Docker socket) is acceptable on this host, or you are on Kubernetes
- [ ] a user in no extra group sees only `public` with `list_scopes`; a user in `payments-team` sees `payments` and `team-payments-team`; a service token without `cerebro:memory.write` does not see `retain`

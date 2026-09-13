# Cerebro v2: everything behind the gateway is a plugin

Status: **proposal, all four open decisions made 2026-09-13** (section 11). Nothing below is implemented. The v1 stack lives on as the `v1` branch; `main` is v2 from here (section 10).

The one-line version: keep the gateway as the single MCP endpoint and the *scope* as the isolation unit, but turn every
engine (docs index, code intelligence, memory, identity, inference, provisioning) into an implementation of a small
contract, driven by one `cerebro.yaml`, with workloads created per unit by a provisioner instead of a code generator.

## 1. What is coupled today

The v1 code is small (about 1,300 lines outside plugins) and already has one real abstraction, the knowledge-source
plugin (`sdk/stack_plugins`). Everything else is wired by name:

| Coupling | Where | Effect |
|---|---|---|
| The gateway *is* the engine clients | `mcp/gateway/gateway/server.py` builds LightRAG, Sourcebot, Hindsight and CodeGraphContext requests inline | Swapping any engine means editing the tool functions |
| Engine addresses are naming conventions | `http://lightrag-{scope}:9621`, `http://codegraph-{scope}:8045/mcp` hard-coded in gateway and ingest | Provisioning, naming and lookup are the same thing |
| Identity is one mechanism | `acl.py` reads two proxy headers; the whole trust model is "only the proxy can reach port 8090" | No tokens, no service accounts, no per-tool scopes |
| Config is five files in three formats | `scopes.yaml`, `stack.env`, `sourcebot/config.json`, `postgres/init.sql`, `proxy/Caddyfile` | The generator has to read and cross-derive all of them (`DERIVED_ENV`) |
| Provisioning is string templating | `scripts/gen-scopes.py` prints compose YAML and k8s manifests from f-strings, one fixed shape per scope | Cannot express "this scope uses TokenSave, that one CodeGraphContext"; no on-demand or scale-to-zero |
| The ingest engine knows LightRAG's quirks | `ingest/ingest/lightrag.py` (basename-only ids, 409 on re-insert, busy pipeline) leaks into `sync.py` | A retrieval-only index cannot be dropped in |
| Sync state is a JSON file on a volume | `ingest/ingest/state.py` | One ingest replica, ever |

None of this was wrong for a v1 that had to prove the access model. It is the wrong shape for a system with
replaceable engines and thousands of users.

## 2. Principles for v2

1. **Ports and adapters.** The gateway and the ingest engine depend only on contracts in a `core` package. Every
   engine is an adapter chosen by `type:` in config. The MCP tool surface agents see does not change when an engine does.
2. **Two kinds of contract.** In-process Python protocols for the gateway's own adapters, and *wire* contracts for
   anything the provisioner runs as a workload (MCP over streamable HTTP plus a capability manifest). The wire
   contracts are language-agnostic by construction, which is what makes "rewrite it in Go later" a real option.
3. **Scope stays the isolation unit.** A graph index merges its inputs, so the boundary must be the index. That
   argument (docs/ACCESS-CONTROL.md) survives the redesign unchanged. What becomes configurable is the *provisioning
   unit* underneath a scope: one workload per scope, or one per repository.
4. **Identity is a contract, not a header.** A request is resolved to a `Principal` by an identity adapter. The
   gateway is always the OAuth 2.1 resource server; who mints tokens is a mode: nobody (single user), a builtin server
   the stack runs, or the organisation's IdP. Trusted proxy headers remain as a legacy adapter.
5. **One config file, secrets excluded.** `cerebro.yaml` is the only thing an operator edits. It references secrets
   by name; it never contains them. Everything currently generated is derived from it by the provisioner.
6. **Nothing leaves the boundary except through the inference adapter.** The org-control rule from v1 stays: any
   adapter that sends text to a model does so through the configured inference backend, and the README table of
   "who sends what" is regenerated from the adapters' declarations.

## 3. The contracts

```
core/
  principal.py     Principal, Grant
  contracts/
    identity.py    IdentityProvider      resolve(request) -> Principal
    policy.py      AccessPolicy          grants(principal) -> Grants  (scopes, repos, banks, tool scopes)
    docs.py        DocumentIndex         apply(scope, batch) / query(scope, q, opts) / health(scope)
    code.py        CodeIntelligence      capabilities(unit) / call(unit, tool, args) / health(unit)
    memory.py      MemoryStore           recall / retain / reflect (bank-addressed)
    sources.py     Source, LiveSource, McpUpstream   (the v1 plugin contract, moved, unchanged)
    inference.py   Inference             chat / embed  (used only by adapters that need a model themselves)
    provision.py   Provisioner           ensure(unit_spec) / release(unit) / endpoint(unit) / status(unit)
    state.py       SyncState             get / commit   (ingest versions)
  config.py        the cerebro.yaml schema (pydantic), one loader, JSON-schema export
```

Sketches of the ones that change the architecture:

```python
class Principal(BaseModel):
    subject: str                  # stable user or service id
    groups: set[str]
    token_scopes: set[str]        # what the *token* may do: docs.read, code.read, memory.read, memory.write, admin
    kind: Literal["user", "service"]

class Grants(BaseModel):          # computed by AccessPolicy, never by a tool
    scopes: list[str]
    repos: list[str]
    banks: list[str]
    personal_bank: str | None

class DocumentIndex(Protocol):
    def apply(self, scope: str, batch: Batch) -> ApplyReport: ...        # deletes + upserts, engine decides ordering
    async def query(self, scope: str, query: str, opts: QueryOptions) -> DocAnswer: ...   # answer, refs, answered: bool
    async def health(self, scope: str) -> Health: ...

class CodeIntelligence(Protocol):
    async def capabilities(self, unit: CodeUnit) -> Capabilities: ...    # {"search": bool, "graph": bool, tools: [...]}
    async def call(self, unit: CodeUnit, tool: str, args: dict) -> ToolResult: ...
    async def health(self, unit: CodeUnit) -> Health: ...

class Provisioner(Protocol):
    async def ensure(self, spec: UnitSpec) -> Endpoint: ...   # idempotent: create or return the running workload
    async def release(self, unit: UnitRef) -> None: ...
    async def status(self, unit: UnitRef) -> UnitStatus: ...
```

The gateway tools become thin: `search_code` asks policy for repos, groups them into code units, asks the code
adapter for each unit's capabilities, calls the `search` tool where it exists, merges. It never knows whether the
unit is Sourcebot, TokenSave or a Zoekt shard.

### Wire contract for provisioned workloads

A workload the provisioner creates for a unit must expose:

- `GET /health`
- MCP over streamable HTTP at `/mcp`, stateless, with **only** read tools enabled
- `GET /.well-known/cerebro-capabilities` returning `{engine, version, unit, capabilities: {search, graph, ...}, tools: [...]}`

That is all the gateway needs. The existing `mcp/codegraph-mcp` image (stdio-to-HTTP bridge in front of `cgc`) is
already this shape minus the manifest, so the pattern is proven.

## 4. Engine assessment

### Code intelligence: is Sourcebot + CodeGraphContext flawed?

Not flawed, but two products doing one job with two isolation models, two licences and two provisioning shapes:

| | Sourcebot | CodeGraphContext | TokenSave | FalkorDB code-graph |
|---|---|---|---|---|
| Answers | exact / regex / symbol text search | call graph, blast radius | call graph, impact, dead code, test map, type hierarchy, semantic search, 40+ tools | call graph, Python / Java / C# only |
| Isolation | one shared index, query-time `repo:` filter | one FalkorDB + server per scope | index file per project (`.tokensave/tokensave.db`), siblings addressable via `graph_root` | one FalkorDB per instance |
| Transport | REST, API key created by hand in the UI | stdio, bridged to HTTP by our image | stdio only, no server mode | stdio only ("HTTP/SSE deferred") |
| Licence | FSL-1.1 | MIT | MIT | MIT app, SSPL database |
| Model calls | none | MiniLM local | none reported | LiteLLM, defaults to Gemini |
| Runtime | Node, needs Postgres + Redis, runs as root | Python + FalkorDB | single Rust binary, libSQL | Python + FalkorDB |

Assessment:

- **TokenSave fits the "scoped pod" idea best.** One binary, index on a volume, incremental `tokensave sync`, no
  database service, no model, MIT. It needs the same stdio-to-HTTP bridge we already have, and a tool allowlist that
  drops its own "memory" tools (they overlap Hindsight) and every edit primitive. It has no auth and no multi-user
  concept, which is fine: the pod is private to one unit and only the gateway reaches it. Its cross-repo support is
  "siblings of the served root", which maps exactly onto a scope pod with the scope's repos checked out side by side.
  Unverified: whether it offers plain regex text search; if not, the pod adds a ripgrep tool and the unit still
  satisfies `search`.
- **CodeGraphContext stays as an adapter.** It works today and is what the demo runs. Under the new contract it is
  one `CodeIntelligence` implementation among several, not the architecture.
- **Sourcebot becomes optional.** It solved "always-fresh search across everything" with a shared index, at the
  price of FSL, a hand-created API key, root, and two extra databases. With per-unit pods that check out the repos
  anyway, a per-unit search tool covers the same need. Keep the adapter for organisations that already run Sourcebot;
  plain Zoekt is the Apache-licensed fallback noted in docs/ENGINES.md.
- **FalkorDB code-graph is the weakest candidate**: three languages, a Gemini default that would have to be re-routed
  through the org's inference endpoint, and an SSPL database. Keep it in the pilot list only if the org is Java/C# heavy.

**Decision (2026-09-13, revised): TokenSave is the only code engine in v2.** `CodeIntelligence` keeps its `search` and
`graph` capabilities so another engine can be reintroduced by implementing the interface, but CodeGraphContext and
Sourcebot are not ported; they stay on the `v1` branch as reference. `search` is served by ripgrep inside the
TokenSave unit image.

### Documents

`DocumentIndex` with three adapters: **LightRAG** (port of `ingest/lightrag.py`, all the quirks stay inside the
adapter), **pgvector retrieval-only** (chunks + embeddings in the shared Postgres, no extraction, cheap ingest),
LazyGraphRAG or another GraphRAG later. The ingest engine's `Batch` becomes the contract's input; the adapter decides
ordering and idle-waits.

**Decision (2026-09-13): LightRAG stays the default.** It is what the PoC verified, and the contract is what makes
the choice cheap to revisit: a GraphRAG or retrieval-only implementation replaces it with one line of config, no
gateway or ingest change. The pgvector adapter is still built, as the proof that the contract holds and as the
low-cost option for installs without a capable model endpoint.

### Memory

`MemoryStore` with Hindsight as the first adapter. Bank addressing (`user-<id>`, `team-<group>`) moves into the
policy layer so a second adapter (Graphiti, Mem0, or a plain pgvector store with the same three verbs) needs no
policy changes. `reflect` is declared as an optional capability; adapters that cannot reason return "unsupported"
and the gateway hides the tool.

## 5. Provisioning: units and pods

A **unit** is what gets its own workload. `cerebro.yaml` names the unit granularity per engine:

```yaml
engines:
  docs:
    type: lightrag
    unit: scope                     # one index per scope (required for graph indexes)
  code:
    type: tokensave
    unit: scope                     # default; a scope may override with code: { unit: repo }
    idle_ttl: 2h                    # scale to zero when unused; ensure() brings it back
    resources: { cpu: "1", memory: 2Gi, storage: 10Gi }

scopes:
  payments:
    code:
      unit: repo                    # this scope wants one workload per repository
      repos:
        - https://github.com/pallets/jinja.git                 # string = default branch only
        - url: https://github.com/pallets/flask.git
          branches: [main, "release/*", stable]                # tracked branches; globs resolved at index time
```

**Decision (2026-09-13): unit granularity is configuration, not architecture.** `engines.code.unit` is `scope` or
`repo`; a scope may override it; and branches are declared per repository. The gateway exposes `branch` as an
optional argument on `search_code` and `code_graph`, defaulting to the repository's default branch.

What TokenSave does with that, verified against its README (v7.3): multi-branch is opt-in per project
(`tokensave branch add` while that branch is checked out; each tracked branch gets its own libSQL database copied
from the nearest ancestor and synced only for the diff); every query takes `graph_root` (absolute root of an
initialised project) and an optional `graph_branch` (must be a tracked branch); and three cross-branch tools exist
(`tokensave_branch_search`, `tokensave_branch_diff`, `tokensave_branch_list`). Graphs are strictly per branch: each tracked
branch is a full database copy, there is no merged cross-branch graph, and cross-branch questions are answered by
`tokensave_branch_diff` and `tokensave_branch_search`. Two constraints shape the indexer: `branch add` tracks the
branch currently checked out and needs a local ref, and worktrees get their own separate `.tokensave/`, which would
split the databases. So the indexer for a unit works in one checkout: for each configured branch, `git checkout`,
`tokensave branch add`, `tokensave sync`; then it returns to the default branch, which is the one the server binds to
at startup. The gateway passes `graph_root` and `graph_branch` on every call; whether every graph tool honours
`graph_branch` (the README documents it as a general query selector) is confirmed in the pilot. TokenSave has no regex text search (its own hook passes regex patterns through to grep), so the unit
image bundles ripgrep behind a `grep` tool to satisfy the `search` capability. CodeGraphContext has neither branches
nor sibling roots: under `unit: repo` it is one FalkorDB per repo, and `branch` is rejected as unsupported by its
capability manifest.

`Provisioner.ensure(spec)` is called by the gateway on first use of a unit and by the indexer on schedule. Two
implementations:

- **compose**: renders `docker/compose.generated.yaml` and runs `docker compose up -d <service>`. Dev only.
- **kubernetes**: a controller that owns a `CerebroUnit` custom resource (or, first version, plain Deployments +
  PVCs + Services with labels, created through the API). Scale-to-zero via the controller's own idle timer or KEDA.
  Because units are addressed through the provisioner's `endpoint()`, the gateway stops embedding service names.

What the engine adapter contributes is a `UnitSpec` template: image, args, env, volumes, ports, the indexer job
command. That is the v1 `McpUpstream` dataclass generalised to every engine, which is why the source-plugin contract
already has the right shape.

Fan-out is the cost of finer units. `search_code` across a user with fifty repos is fifty MCP calls; the gateway
bounds concurrency per request and per unit, and the unit manifest can declare `search_many` for engines that
handle several roots in one call (TokenSave's `graph_root` is that).

## 6. Identity and authorization

Two roles, kept apart on purpose:

- The gateway is always the **resource server**. It receives a bearer token, validates it, turns it into a
  `Principal`, and asks `AccessPolicy` what that principal may see. `list_scopes` (plus a new `whoami`) shows the
  result to the agent. This code path is identical no matter who issued the token.
- The **authorization server**, the thing users log into and that mints tokens, is a deployment choice with three
  modes. The gateway never mints tokens itself.

| `identity.mode` | Who mints tokens | Meant for | What the stack runs |
|---|---|---|---|
| `none` | nobody | one person on their own machine | nothing. Every request resolves to the fixed `principal:` from config with every grant. The gateway binds to loopback unless `allow_remote: true`, which then requires a static bearer token so a LAN exposure is never open. |
| `builtin` | an authorization server the stack provisions | a team self-hosting with no IdP | one shared unit running an off-the-shelf server; users, groups and passkeys in the shared Postgres; `users:` from `cerebro.yaml` seeded on first start |
| `external` | the organisation's identity provider | enterprises | nothing extra. Protected-resource metadata points at the issuer; groups come from a claim or from userinfo |

`builtin` and `external` run the same gateway code with a different issuer URL. That is the point: the builtin server
is a convenience, not a second security model.

### What "modern and standard" means today

The MCP authorization specification (2025-11-25 revision, extended in 2026) fixes the list. The gateway implements
the MUSTs; the builtin server has to be chosen so it satisfies the SHOULDs.

| Standard | Role | Where it lands |
|---|---|---|
| OAuth 2.1 with PKCE | baseline; implicit and password grants are gone | every mode |
| RFC 9728 Protected Resource Metadata | MUST: `/.well-known/oauth-protected-resource` and the 401 challenge | gateway |
| RFC 8414 Authorization Server Metadata | discovery of the issuer's endpoints | builtin / external server |
| RFC 8707 Resource Indicators | tokens are minted *for this gateway*; any other audience is rejected | gateway validates, server must honour |
| Client ID Metadata Documents (CIMD) | SHOULD, the preferred way an MCP client identifies itself: `client_id` is a URL to a JSON document | builtin server must support; external is the org's IdP's job |
| RFC 7591 Dynamic Client Registration | MAY, deprecated in the spec; fallback for clients that only speak DCR | builtin server |
| Enterprise-Managed Authorization (identity-assertion grant, "cross-app access") | the IdP mints the MCP token without a per-user redirect flow | external mode; the gateway only validates what the IdP issues |
| Passkeys (WebAuthn) | user login at the builtin server | builtin server |
| RFC 9449 DPoP | sender-constrained tokens, optional hardening | later, gateway and server |
| RFC 8693 Token Exchange | per-user tokens toward engines instead of shared service credentials | later; engines use service credentials today |
| SCIM 2.0 | push groups from an enterprise IdP into the builtin server | optional, builtin server |

Client registration (CIMD, DCR) happens between the MCP client and the authorization server; the gateway never sees
it. That is why the builtin server choice matters more than any gateway code here.

### Choosing the builtin server

Do not write one. An authorization server is where security bugs concentrate, and maintained ones with permissive
licences exist. Criteria: OAuth 2.1 and OIDC, RFC 8414, RFC 8707, CIMD or at least DCR, passkeys, Postgres storage,
a footprint a laptop tolerates. **Decision (2026-09-13): Keycloak** (Apache-2.0) is the first `builtin` implementation: the most complete of the
candidates (DCR, passkeys, OIDC, fine-grained admin all known to work) and the one most organisations already know;
its footprint (about 1 GB RAM) costs teams, not home users, who run `mode: none`. Authentik and Ory Hydra with Kratos
remain possible second adapters. Two things to verify during the identity slice: RFC 8707 resource indicators on the
pinned Keycloak version, and CIMD; if Keycloak lacks CIMD, the gateway serves a small CIMD-to-DCR shim so spec-following
MCP clients still register without manual steps. The adapter is `AuthorizationServer` with a unit template, so the
provisioner runs it like any engine, plus `seed(users, groups)` against Keycloak's admin API (realm, groups, users,
the gateway as a resource client with audience mapper).

### Identity adapters in the gateway

| type | Principal from |
|---|---|
| `bearer_jwt` | JWT claims: `sub`, a configurable groups claim, `scope`; keys from the issuer's JWKS (builtin and external) |
| `bearer_introspect` | RFC 7662 introspection, for opaque tokens |
| `none` | the fixed `principal:` in config |
| `trusted_headers` | `X-Forwarded-User` / `X-Forwarded-Groups` behind a proxy that does the OIDC flow; the v1 model, kept for orgs whose edge already works this way |
| `static` | a YAML map of tokens to principals, for tests and the demo |

Token scopes are per capability: `cerebro:docs.read`, `cerebro:code.read`, `cerebro:memory.read`,
`cerebro:memory.write`, `cerebro:admin`. Every tool declares the scope it needs; the gateway hides tools the token
cannot use. Group membership still decides *which* scopes and banks (that is `AccessPolicy`, the v1 yaml mapping
ported). Service accounts are client-credentials tokens with `kind: service` and no personal bank, which is what a
CI agent needs.

Two things this does not solve, said plainly: engines still hold shared service credentials (the gateway is the only
caller, as before), and revocation latency is the token lifetime unless introspection is used.

## 7. One configuration file

```yaml
# cerebro.yaml — the only file an operator edits. Secrets are referenced, never written here.
version: 2

identity:
  mode: external            # none | builtin | external
  issuer: https://sso.internal/realms/eng
  audience: https://context.internal/mcp
  groups_claim: groups
  token_validation: jwks    # jwks | introspection
  # mode: none              # one user at home: no tokens, gateway on 127.0.0.1
  # principal: { subject: me, groups: [everyone, admin] }
  # mode: builtin           # the stack runs an authorization server as a unit
  # server: { type: keycloak }                          # decided: Keycloak is the first builtin adapter
  # users: [{ name: alice, groups: [payments-team] }]   # seeded once; passkey set at first login
  # legacy: { type: trusted_headers, user_header: X-Forwarded-User, groups_header: X-Forwarded-Groups }
policy:
  type: groups            # groups -> scopes, from `scopes:` below
  always_groups: [everyone]
  team_banks_from_groups: true

inference:                # the only outbound path for document and memory text
  llm:   { provider: ollama, base_url: http://ollama:11434, model: gpt-oss:20b }
  embed: { provider: ollama, base_url: http://ollama:11434, model: bge-m3, dim: 1024 }

engines:
  docs:   { type: lightrag, unit: scope, options: { max_async: 2, gleaning: 0, think: false } }
  code:   { type: tokensave, unit: scope, idle_ttl: 2h }     # the only code engine in v2
  memory: { type: hindsight, options: { reranker: local } }

provisioning:
  target: kubernetes      # or compose
  namespace: context-stack
  storage_class: default

secrets:                  # names only; resolved from env / k8s Secret / an external store adapter
  source: env             # or: kubernetes, vault
  keys: [POSTGRES_PASSWORD, LIGHTRAG_API_KEY, HINDSIGHT_API_KEY, CONFLUENCE_TOKEN, GITHUB_TOKEN]

sources:                  # non-secret options for knowledge-source plugins (unchanged)
  jama: { url: https://jama.internal }

scopes:                   # unchanged: the isolation unit for docs AND code
  public:
    groups: [everyone]
    code: { repos: [https://github.com/pallets/click.git] }
    docs: { confluence: { spaces: [ENG] }, git: {} }
```

The loader validates against a pydantic schema and exports JSON schema for editor completion. `sourcebot/config.json`,
`postgres/init.sql`, the compose files and the k8s manifests are all *outputs* of the provisioner. `stack.env` shrinks
to secrets only, and only for the `env` secrets source.

## 8. Is Python still the right language?

For the gateway, yes, for now. It is an I/O-bound proxy: every tool call is a network round trip to an engine or a
model, and the model is the bottleneck by two orders of magnitude. Async Python behind N replicas serves thousands of
users; the v1 gateway is already stateless per request, so it scales horizontally without change. The things that
would push toward Go or Rust are not throughput:

- a real Kubernetes controller (the `kubernetes` provisioner) is naturally written with controller-runtime; the
  Python `kopf` route works for a first version and is what I would start with
- a per-unit sidecar that must be tiny and start in milliseconds (the stdio-to-HTTP bridge) is a good Go program

**Decision (2026-09-13): the Kubernetes provisioner starts in Python with `kopf`.** Same language and repo as the
gateway, it shares the `cerebro.yaml` schema and the adapters' unit templates directly, and it is the fastest path to
an on-demand `ensure()` with idle TTL. A Go controller-runtime rewrite is warranted only if it grows into a real
operator with CRDs and reconciliation at a scale hundreds of units do not reach.

The wire contracts in section 3 are what keep the language choice reversible: a Go gateway would implement the same
tools against the same unit endpoints, and adapters that are workloads never cared what the gateway is written in.

## 9. Repository layout

```
cerebro.example.yaml            the one config (copy to cerebro.yaml, which is gitignored)
cerebro/core/                   contracts, Principal, config schema, registry, units — imports no engine
cerebro/gateway/                MCP server: tools + fan-out + identity + policy; imports core only
cerebro/ingest/                 sync engine; imports core only
cerebro/adapters/<kind>/<name>  identity/ auth/ policy/ docs/ code/ memory/ inference/ provision/ state/
cerebro/sdk/                    what plugins/*.py import (the v1 stack_plugins names)
plugins/                        knowledge sources (Confluence, Backstage, git, files, Jama), unchanged contract
images/                         Dockerfiles cerebro builds: gateway, ingest, code unit (TokenSave + ripgrep + bridge)
deploy/                         hand-written base for compose / kubernetes; deploy/generated is rendered, gitignored
tests/contracts/                the harness every adapter must pass; tests/<component>/ next to it
```

Adapters are selected by `type:` and resolved by convention (`cerebro.adapters.<kind>.<type>:Adapter`) or as
`module:Class` for an out-of-tree pip package, so no shared registry file exists to conflict over.

## 10. Branches and build order

**Branching.** Today's `main` becomes the `v1` branch, frozen as the reference for the v1 stack: the demo, the
verified engine facts (README "Verified versions") and the access-control argument. Bug fixes only, and only if
someone is running it. `main` continues as v2 from the same commit; nothing is rewritten, v1 code is removed in the first v2
commit (the skeleton) so that `main` is unambiguously v2; ports read their sources from the `v1` branch (`git show v1:<path>`). An orphan `main` is the alternative
if a clean history is preferred; keeping it is recommended because the v1 branch's `git log` explains many engine quirks
that the adapters will inherit.

Dropping the "every step keeps `make demo` working" constraint is the main reason to split the branches: v2 can be
built contracts-first instead of being refactored out of v1. The v1 smoke test is still the acceptance bar
for each vertical slice below.

1. **Skeleton.** The layout in section 9, the `core` contracts, the `cerebro.yaml` schema with loader and JSON schema,
   a contract-test harness every adapter must pass.
2. **Empty gateway.** `identity.mode: none` and `static`, `whoami` and `list_scopes`, no engines. A runnable product.
3. **Docs slice.** LightRAG adapter ported from `v1`, the ingest engine with Postgres sync state, the source plugins
   copied from `v1`. First end-to-end query.
4. **Memory slice.** Hindsight adapter.
5. **Identity.** `bearer_jwt` with RFC 9728 metadata and the 401 challenge; the `builtin` server as a provisioned
   unit; `external` verified against the chosen authorization server. The smoke test gains a token mode.
6. **Code slice.** `CodeIntelligence`, the unit image (TokenSave binary + ripgrep + stdio-to-HTTP bridge with the
   capability manifest), the TokenSave adapter and indexer; the compose provisioner.
7. **Kubernetes provisioner.** On-demand `ensure()`, idle TTL, scale to zero.
8. **Second adapters.** pgvector retrieval-only docs adapter (proves the contract); demo scripts retargeted at
   `cerebro.yaml`.
9. **Docs.** README, ENGINES and the "what the model sees" table regenerated from adapter declarations; the `v1`
   README gets a pointer to `main`.

## 11. Decisions

- ~~Unit granularity default~~ **Decided 2026-09-13**: configurable per engine and per scope (`scope` | `repo`),
  branches per repository (section 5).
- **Code engine (revised 2026-09-13)**: TokenSave only; CodeGraphContext and Sourcebot are not ported (section 4).
- ~~Builtin authorization server~~ **Decided 2026-09-13**: Keycloak first (section 6); RFC 8707 and CIMD verified
  during the identity slice, CIMD-to-DCR shim in the gateway if needed.
- ~~Docs default engine~~ **Decided 2026-09-13**: LightRAG stays the default; GraphRAG or retrieval-only are
  drop-in replacements through `DocumentIndex` (section 4).
- ~~Language of the Kubernetes provisioner~~ **Decided 2026-09-13**: Python `kopf` first (section 8).

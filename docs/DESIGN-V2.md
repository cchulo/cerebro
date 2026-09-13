# Cerebro v2: everything behind the gateway is a plugin

Status: **proposal**, 2026-09-13. Nothing below is implemented; the current stack keeps working while this is discussed.

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
4. **Identity is a contract, not a header.** A request is resolved to a `Principal` by an identity provider adapter.
   OAuth 2.1 bearer tokens are the default; trusted proxy headers remain as a second adapter for the SSO-edge deployment.
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

Recommendation: `CodeIntelligence` contract with `search` and `graph` capabilities; adapters for TokenSave (new,
pilot first), CodeGraphContext (port), Sourcebot (port); default in `cerebro.yaml` stays CodeGraphContext until the
TokenSave pilot passes the smoke test.

### Documents

`DocumentIndex` with three adapters: **LightRAG** (port of `ingest/lightrag.py`, all the quirks stay inside the
adapter), **pgvector retrieval-only** (chunks + embeddings in the shared Postgres, no extraction, cheap ingest, the
default for a first deployment), LazyGraphRAG later. The ingest engine's `Batch` becomes the contract's input; the
adapter decides ordering and idle-waits.

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
    unit: repo                      # or: scope  (repos as siblings in one pod)
    idle_ttl: 2h                    # scale to zero when unused; ensure() brings it back
    resources: { cpu: "1", memory: 2Gi, storage: 10Gi }
```

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

The gateway becomes an **OAuth 2.1 resource server** exactly as the MCP authorization specification describes:

- `GET /.well-known/oauth-protected-resource` (RFC 9728) names the authorization server(s) and the scopes
- unauthenticated requests get `401` with `WWW-Authenticate: Bearer resource_metadata="..."`
- tokens are validated for audience (RFC 8707 resource indicator) and signature (JWKS) or by introspection (RFC 7662)
- the gateway issues nothing itself

The authorization server is external and swappable: Keycloak, Authentik, Dex, Okta, Entra, whatever the org runs.
"Support OAuth 2.0" therefore means implementing the resource-server side well, plus a policy layer, not writing an
identity server.

`IdentityProvider` adapters:

| type | Use | Principal from |
|---|---|---|
| `oauth2_jwt` | default; MCP clients that speak the spec (Claude Code, Cursor do) | JWT claims: `sub`, groups claim (configurable), `scope` |
| `oauth2_introspect` | opaque tokens | introspection response |
| `trusted_headers` | behind oauth2-proxy / Caddy OIDC, the v1 model | `X-Forwarded-User`, `X-Forwarded-Groups` |
| `static` | tests and the demo | a YAML map of tokens to principals |

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
  type: oauth2_jwt
  issuer: https://sso.internal/realms/eng
  audience: https://context.internal/mcp
  groups_claim: groups
  # type: trusted_headers  { user_header: X-Forwarded-User, groups_header: X-Forwarded-Groups }

policy:
  type: groups            # groups -> scopes, from `scopes:` below
  always_groups: [everyone]
  team_banks_from_groups: true

inference:                # the only outbound path for document and memory text
  llm:   { provider: ollama, base_url: http://ollama:11434, model: gpt-oss:20b }
  embed: { provider: ollama, base_url: http://ollama:11434, model: bge-m3, dim: 1024 }

engines:
  docs:   { type: lightrag, unit: scope, options: { max_async: 2, gleaning: 0, think: false } }
  code:   { type: tokensave, unit: scope, idle_ttl: 2h }
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

The wire contracts in section 3 are what keep the language choice reversible: a Go gateway would implement the same
tools against the same unit endpoints, and adapters that are workloads never cared what the gateway is written in.

## 9. Repository layout

```
cerebro.yaml                    the one config (example committed; real one gitignored)
core/                           contracts, Principal, config schema — no engine imports
gateway/                        MCP server: tools + fan-out + policy; imports core only
ingest/                         sync engine; imports core only
adapters/
  identity/   oauth2_jwt.py oauth2_introspect.py trusted_headers.py static.py
  docs/       lightrag/  pgvector/
  code/       tokensave/  codegraphcontext/  sourcebot/       (each: adapter.py + unit template + image/)
  memory/     hindsight/
  inference/  ollama.py openai_compat.py
  provision/  compose.py  kubernetes/
  state/      json_file.py  postgres.py
plugins/                        knowledge sources (Confluence, Backstage, git, files, Jama), unchanged contract
deploy/                         hand-written base: postgres, redis, ollama, gateway, ingest
scripts/                        up/down/status/demo/smoke, retargeted at cerebro.yaml
tests/                          contract tests every adapter must pass, plus the smoke test
```

Adapters are selected by `type:` and resolved through entry points (`cerebro.adapters.docs = lightrag = adapters.docs.lightrag:LightRAG`),
so an out-of-tree adapter is a pip package, the same way the live loader already accepts `module:Class`.

## 10. Migration, in order

Each step keeps `make demo` working and lands as its own commit.

1. **`core` + config.** Add the contracts and the `cerebro.yaml` schema; write a converter from the five v1 files.
   No behaviour change.
2. **Identity adapters.** Extract `acl.py` into `IdentityProvider` + `AccessPolicy`; `trusted_headers` is the port,
   `oauth2_jwt` is new, with `/.well-known/oauth-protected-resource` and the 401 challenge. Smoke test gains a token mode.
3. **Memory and docs adapters.** Move Hindsight and LightRAG code out of the gateway and ingest into `adapters/`;
   add `pgvector` retrieval-only. Ingest state moves to Postgres (`SyncState`), removing the single-replica limit.
4. **Code adapters + unit manifest.** Port CodeGraphContext and Sourcebot behind `CodeIntelligence`; add the
   capability manifest to the bridge image. `search_code` and `code_graph` become capability-driven.
5. **Provisioner.** Replace `gen-scopes.py` with the compose provisioner (same output, new source of truth), then the
   Kubernetes provisioner with on-demand `ensure()` and idle TTL.
6. **TokenSave adapter and pilot.** New image (binary + bridge + ripgrep), contract tests, run the demo with
   `engines.code.type: tokensave`, compare against CodeGraphContext on the demo prompts.
7. **Retire** whatever the pilot makes redundant; update ENGINES.md and the README "what the model sees" table from
   adapter declarations.

## 11. Decisions I would like from you

- **Unit granularity default**: per scope (fewer pods, cross-repo graph inside a scope) or per repo (finest
  isolation, most pods)? I recommend per scope as the default with per-repo opt-in.
- **Authorization server for the pilot**: Keycloak is the safest self-hosted choice for the demo; if the org already
  runs one, name it so the JWT adapter is verified against the real claim shape.
- **Docs default engine**: keep LightRAG, or make retrieval-only the default and LightRAG the opt-in? The latter makes
  first deployments cheap and matches what you asked for earlier.
- **Language of the Kubernetes provisioner**: Python `kopf` first, Go later, or Go from the start?

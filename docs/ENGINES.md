# The engines and their contracts

Every engine is an adapter of one contract in `cerebro/core/contracts/`, chosen by `type:` in `cerebro.yaml` and
resolved as `cerebro.adapters.<kind>.<type>:Adapter` or `module:Class`. Each adapter also declares what the
provisioner should run for it (`units()`, `jobs()`). The tests every adapter must pass are in `tests/contracts/`.

| Question | Answer |
|---|---|
| Why one document index per scope? | a graph index merges knowledge across its inputs at index time; filtering results afterwards leaks; the boundary has to be the index ([ACCESS-CONTROL.md](ACCESS-CONTROL.md)) |
| Why not put the docs into memory? | Hindsight is what agents learned by doing; a document store is what the docs say; mixing them ruins both |
| Which engines call the model? | LightRAG and Hindsight, through the `inference:` endpoints. TokenSave, ripgrep and Keycloak do not |
| Why did Sourcebot and CodeGraphContext go? | two products, two isolation models, two licences for one job; TokenSave is one MIT binary with per-repository, per-branch indexes and no service. They stay on the `v1` branch as reference ([DESIGN-V2.md](DESIGN-V2.md), section 4) |
| Can I swap an engine? | yes, with one `type:` line, as long as the replacement implements the contract and passes its harness |

## DocumentIndex

`apply(scope, Batch)`, `query(scope, query, QueryOptions) -> DocAnswer`, `health(scope)`, optional `stats(scope)`;
`modes` and `default_mode` name the engine's query modes. A `Batch` is one scope's deletes and upserts; the adapter
decides ordering. `DocAnswer.answered` is the contract's honesty flag: `False` means "the index had nothing useful",
and the gateway then falls back to live sources.

**LightRAG** (`type: lightrag`, default; `ghcr.io/hkuds/lightrag:v1.5.7`). One server per scope, unit `docs-<scope>`,
port 9621, storage in the shared Postgres (KV, doc status, vectors and graph in database `lightrag`, one `WORKSPACE`
per scope), a data volume sized by `engines.docs.resources.storage`. Modes `local`, `global`, `hybrid`, `mix`
(default), `naive`. What the adapter hides: `file_source` is the document identity and LightRAG keeps only its
basename, so `/` in source ids travels as `|`; re-inserting an existing source is a 409, so changes are delete then
insert; deletes run in the background and refuse while the pipeline is busy, so a batch is wait, list, delete, insert
in chunks of 25; the query-answer cache is off (it returned stale answers after a sync on 1.5.7) while the
per-chunk extraction cache stays on. Tuning through `options`: `max_async` 2, `max_parallel_insert` 1, `gleaning` 0,
`think` false, `max_output_tokens` 4096. Verified in v2: unit tests against mocks of the same calls v1 verified live.

**pgvector retrieval-only** is designed (chunks and embeddings in the `cerebro` database, no extraction, cheap ingest,
`answer` = concatenated passages, for installs without a capable model endpoint) and has the `pgvector` pip extra and
the `Inference` contract it would use, but no adapter is on `main` yet.

**Writing another** (GraphRAG, a vector store with document-level security): subclass `DocumentIndex`, keep every
document inside the `scope` you are given, map your engine's "nothing found" onto `answered=False`, declare `modes`,
return `UnitSpec`s from `units()` if the engine is a workload, and pass `tests/contracts/docs.py`.

## CodeIntelligence

`capabilities(unit) -> Capabilities` (`search`, `graph`, `branches`, `multi_root`, the read-only `tools`),
`search(unit, query, repos, branch, regex, max_results) -> [SearchHit]`, `call(unit, tool, args, branch) -> ToolResult`,
`health(unit)`. A unit is a scope's repositories side by side or one repository (`cerebro.core.units`). `repos`
narrows, never widens; a `branch` on an engine without the capability is `Unsupported`.

**TokenSave** (`type: tokensave`, the only implementation; `7.12.1`, pinned and SHA256-checked in
`images/code-unit/Dockerfile`). The adapter never runs TokenSave itself: it talks to the unit's **bridge**
(`cerebro bridge serve`, `cerebro/bridge/`) over the wire contract, `GET /health`, `GET /.well-known/cerebro-capabilities`,
`POST /mcp`. Per unit: image `cerebro/code-unit:<cerebro version>`, port 8045, env `CEREBRO_UNIT`, `CEREBRO_REPOS`
(JSON `[{url, branches}]`), a `workspace-<unit>` volume at `/workspace` shared with the `index-<unit>` job
(`cerebro index run --unit <name>`, cron `engines.code.options.schedule`, default `0 3 * * *`).

The unit layout, verified with the real binary (`cerebro/bridge/workspace.py`, `images/code-unit/README.md`):

```
/workspace/.cerebro-root/          the project TokenSave SERVES: initialised, empty, never holds code
/workspace/<dir>/                  one checkout per repository, default branch checked out
/workspace/<dir>/.tokensave/       its index; tracked branches under branches/<b>.db
/workspace/<dir>/.cerebro-index.json   what the indexer last did (default branch, branch -> commit)
```

`graph_root` may only name a project other than the served one and `graph_branch` is refused for the served project,
so serving an empty root makes every repository a sibling reached the same way: `graph_root=/workspace/<dir>` plus
`graph_branch=<tracked branch>`, and those opens are read-only, so the index job can run while the bridge serves.
The bridge rewrites each exposed tool's schema to take `repo` and `branch` instead, strips any selector a caller
sends, and fans a call without `repo` out to every repository of the unit (`multi_root`).

Which tools are exposed and why: of TokenSave's 84 tools, 74 carry `readOnlyHint`; of those, the 53 that take the
selectors are exposed. Not exposed: the ten edit / session / memory tools (never), and the 21 read-only tools without
selectors, among them the three `tokensave_branch_*` tools, the VCS tools (diff, log, blame), diagnostics, runtime,
dependencies, redundancy, config and `tokensave_session_recall`; they would only ever see the empty root. Branches
are listed by the bridge's own `unit_info`, per-branch graphs are reached with `branch` on every other tool, and the
`search` capability is the bridge's own `grep`: ripgrep on the checkout for the default branch, `git grep <ref>` for
any other indexed branch (no checkout moves under the running engine), only branches the indexer recorded. Network:
`upload_enabled = false` in the image's `~/.tokensave/config.toml` and `TOKENSAVE_UPDATE_CHECK=off`; nothing leaves
the unit. There is no daemon or shared-server mode, so one `tokensave serve` per bridge, restarted on failure.

**A replacement engine** implements `CodeIntelligence` in-process (any HTTP API works), or is a stdio MCP server
dropped into the same image: subclass `cerebro.bridge.engine.StdioEngine` (`command()`, `root_param`, `branch_param`,
`denied`, `index_marker`) and run the bridge with `--engine module:Class`; the manifest then declares what it can
do and the same adapter serves it. It must expose read-only tools only, address repositories by an absolute root, and
pass `tests/contracts/code.py`.

## MemoryStore

`recall(bank, query, budget, max_tokens)`, `retain(bank, content, context, tags)`, optional `reflect(bank, query, budget)`
(`supports_reflect = False` hides the tool), `health()`. Budgets are `low`, `mid`, `high`. The store only ever sees a
bank name; the policy chooses it.

**Hindsight** (`type: hindsight`; `ghcr.io/vectorize-io/hindsight:0.9.2`), unit `memory`, port 8888 (control plane
on 9999), database `hindsight` in the shared Postgres. Calls: `POST /v1/{tenant}/banks/{bank}/memories/recall`,
`POST .../memories` with `async: true` (extraction is queued, the agent never waits), `POST .../reflect`, `GET /health`;
every call carries `Authorization: Bearer <HINDSIGHT_API_KEY>`, which is also the unit's tenant key, so only the
gateway can reach it. A 404 means the bank was never written to and is answered as "bank is empty". Options:
`reranker` `local` (cross-encoder, fetched once) or `rrf` (no download), `tenant`, `timeout`, `num_ctx`,
`cp_access_key_secret`. Verified in v2: unit tests against mocks of the v1 wire calls.

## AuthorizationServer

`issuer()`, `seed(users, groups, resource_id)`, `ready()`; only used in `identity.mode: builtin`. **Keycloak**
(`type: keycloak`; `quay.io/keycloak/keycloak:26.7.3`), unit `auth`, port 8080, database `keycloak`. What `seed()`
creates and the RFC 8707 / CIMD findings are in [IDENTITY.md](IDENTITY.md). Verified: unit tests against a stateful
mock of the admin REST API.

## Provisioner

`ensure(UnitSpec) -> Endpoint`, `release(unit)`, `status(unit)`, `run_job(JobSpec, wait)`, `render(units, jobs)`,
`touch(unit)`, and `endpoint(unit)` as the Locator every adapter uses. `cerebro/provision/plan.py` assembles the
base units (`postgres` on `pgvector/pgvector:pg16`, `ollama` on `ollama/ollama:0.33.3` when the inference endpoints
point at it, `gateway`, `ingest`) plus every adapter's units and jobs and the `mcp-<plugin>` upstreams, and
normalises `${NAME}` secret references.

| Target | Renders | Runs with | Notes |
|---|---|---|---|
| `compose` | `deploy/generated/compose.yaml` | `docker compose -p <project> --env-file secrets.env` | jobs under profile `jobs`; a `scheduler` (busybox crond in `docker:27-cli` with the Docker socket) for cron jobs; only the gateway port published |
| `kubernetes` | `deploy/generated/k8s/` (namespace, one file per unit and job, kustomization with `secretGenerator` from `secrets.env`) | `kubectl apply -k` and the API client; `cerebro provision operator` (kopf) for idle TTL | plain Deployments / StatefulSets / Services / PVCs / CronJobs; Ingress is yours; verified on rendered objects, not a live cluster |

## SyncState and Inference

`SyncState` (`get`, `keys_with_prefix`, `commit`) remembers the last version of every source item so the ingest
re-sends only changes: `json_file` (one file on the ingest volume, one replica) or `postgres` (table
`cerebro_sync_state` in database `cerebro`, several replicas). Chosen by `gateway.state_type`.

`Inference` (`chat`, `embed`, `health`) is what cerebro itself would use for a model; the engines get the same
endpoints through their unit env instead. `ollama` speaks Ollama's native API, `openai_compat` any `/v1` chat and
embeddings server (`/v1` appended for Ollama, placeholder key `ollama` when none is configured). Both verified
against mocks.

## Licences

| Component | Licence |
|---|---|
| cerebro | MIT |
| LightRAG | MIT |
| TokenSave | MIT |
| ripgrep | MIT / Unlicense |
| Hindsight | MIT |
| Keycloak | Apache 2.0 |
| Postgres + pgvector | PostgreSQL |
| Ollama | MIT |
| mcp-atlassian | MIT |
| kopf, FastMCP (`mcp`), FastAPI | MIT |
| kubernetes client | Apache 2.0 |
| Starlette | BSD 3-Clause |

Check the licence text of the pinned versions before production; this table records the state on 2026-09-13.
The FSL (Sourcebot) and SSPL (FalkorDB) components of v1 are gone from v2.

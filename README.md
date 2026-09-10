# agent-context-stack

Self-hosted context stack for AI agents:

| Layer | Engine | Purpose |
|---|---|---|
| Gateway | `./mcp/gateway` (this repo) | The one MCP endpoint agents use; enforces per-user access |
| Memory | [Hindsight](https://github.com/vectorize-io/hindsight) | What happened before: conversations, agent runs, outcomes (banks per user/team) |
| Documents | [LightRAG](https://github.com/HKUDS/LightRAG), **one instance per scope** | What the docs say: Confluence, Backstage, repo docs, ADRs |
| Code search | [Sourcebot](https://github.com/sourcebot-dev/sourcebot) | Exact / symbol search across remote repos, repo-filtered per user |
| Code graph | [CodeGraphContext](https://github.com/CodeGraphContext/CodeGraphContext) + FalkorDB, **one pair per scope** | Call graph, blast radius (runs as `codegraph-<scope>`, exposed as the `code_graph` tool) |
| Live fallback | each plugin's live part, e.g. Confluence through [mcp-atlassian](https://github.com/sooperset/mcp-atlassian) (declared by the plugin, run as `mcp-confluence`) | When a scope's index has no answer: search/read the system of record, confined to the caller's scopes |
| Ingest | `./ingest` (this repo) | Syncs any document source into the right scope through plugins (`plugins/`: Confluence, Backstage, git docs, files shipped; add your own the same way) |
| Shared | Postgres + pgvector, Ollama, Redis | One DB, one local model endpoint |

**Access control is built in**: `config/scopes.yaml` maps IdP groups → scopes → code repos + document sources. Each scope
is an isolated docs graph and code graph; the gateway only queries the scopes the caller belongs to. Read
[docs/ACCESS-CONTROL.md](docs/ACCESS-CONTROL.md) before adding anything sensitive.

## What the model sees (and where data can leave)

Nothing in this stack talks to a cloud service by itself. The **inference backend** configured in `config/stack.env` is the
only place document or memory text is sent for processing:

| Who sends text to the model | What text | When |
|---|---|---|
| LightRAG (per scope) | every ingested Confluence page, Backstage entity, repo doc; retrieved chunks at query time | ingest, `query_docs` |
| Hindsight | what agents `retain` (outcomes, decisions), recalled memories during `reflect`; embeddings of memories | `retain`, `reflect`, background consolidation |
| Sourcebot, CodeGraphContext | **nothing** — code search and the code graph use no LLM (CodeGraphContext's `find_code` embeds with a bundled MiniLM model) | — |

`LLM_PROVIDER=ollama` keeps it on this host. `LLM_PROVIDER=openai` sends it to any OpenAI-compatible endpoint your
organisation controls or has approved (Azure OpenAI in your tenant, Bedrock/Vertex behind a LiteLLM proxy, internal
vLLM, an approved vendor with a zero-retention agreement) — set `LLM_BASE_URL`/`LLM_API_KEY` and the same for
`EMBED_*`. Everything else stays inside the compose network:

- Product telemetry: Sourcebot's PostHog telemetry is disabled (`SOURCEBOT_TELEMETRY_DISABLED=true`) and its separate
  "service ping" (org stats to deployments.sourcebot.dev, not covered by that flag) is pointed at an unroutable
  address (`SOURCEBOT_LIGHTHOUSE_URL=http://127.0.0.1:9`; expect a logged "lighthouse unreachable" error); Hindsight and
  LightRAG have none (only opt-in OpenTelemetry export, not configured). CodeGraphContext calls a vendor only if you
  set an `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`, which this stack never does.
- One-time model downloads: Ollama pulls models from ollama.com; Hindsight's `local` reranker fetches a small
  cross-encoder from HuggingFace at first start (set `HINDSIGHT_RERANKER=rrf` for zero downloads).
- Ingest reads from Confluence, Backstage and your code host; it never writes to them.
- Every published port is bound to `127.0.0.1`; only the SSO proxy reaches the gateway.

## Layout

```
docker/compose.yaml     shared services + Hindsight + Sourcebot + ingest + gateway
docker/compose.scopes.yaml  GENERATED (make gen, gitignored) per-scope LightRAG / FalkorDB / CodeGraph / indexer services
docker/compose.*.yaml   overrides: host Ollama, NVIDIA GPU, test environment
config/stack.env        secrets + inference backend (copy from config/stack.env.example; gitignored)
config/scopes.yaml      access scopes: groups -> code repos + document sources, one unit for both (edit, then regenerate)
plugins/                one file per source: ingest part + live fallback + MCP upstream image, auto-discovered (docs/PLUGINS.md)
sdk/                    stack_plugins, the framework plugin files use
config/proxy/           example SSO reverse-proxy config
scripts/gen-scopes.py   regenerates docker/compose.scopes.yaml and k8s/generated/ from config/
scripts/up.sh, down.sh  lifecycle for compose and Kubernetes (flags in the file headers; make up/down/nuke)
k8s/                    Kubernetes: base/ hand-written, generated/ from config/ (gitignored)
config/postgres/        creates hindsight / lightrag / sourcebot DBs + pgvector
config/sourcebot/       which repos Sourcebot indexes
ingest/                 FastAPI service: scheduled + webhook sync into LightRAG
mcp/gateway/            identity-aware MCP gateway (docs, code, memory tools)
mcp/codegraph-mcp/      CodeGraphContext image with HTTP MCP bridge (used per scope)
docs/CONNECT.md         how to connect Claude Code / Cursor
docs/ACCESS-CONTROL.md  the scope model and its limits
index/index-repo.sh     clone + CodeGraphContext (+ optional SCIP) index for one or all repos
scripts/pull-models.sh  pulls the Ollama models
```

## Quick start (Docker Compose)

Full walkthrough, including what every file in `config/` is for: **[docs/SETUP.md](docs/SETUP.md)**.
Diagrams: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**. Why these engines, search vs graph, the CodeGraph name
collision, memory vs documents, licences: **[docs/ENGINES.md](docs/ENGINES.md)**.
Adding a source (JAMA, exports, internal APIs) is one file in `plugins/`: **[docs/PLUGINS.md](docs/PLUGINS.md)**; how ingest
and fallbacks relate and stay fresh: **[docs/SOURCES.md](docs/SOURCES.md)**.


```sh
pip install -r scripts/requirements.txt   # pyyaml + mcp client for the generator and the smoke test
cp config/stack.env.example config/stack.env   # secrets + inference backend (see docs/SETUP.md)
edit config/scopes.yaml            # groups -> spaces + repos
make gen                           # writes docker/compose.scopes.yaml and k8s/generated/
make up EXTRA="-f docker/compose.host-ollama.yaml"   # or plain `make up` to run Ollama in the project (then `make models`)
make index                         # first code-graph index, one indexer job per scope
make sync                          # Confluence / Backstage / repo docs -> LightRAG
make smoke                         # access-control checks against the gateway (shows stack activity in green while it runs)
make status                        # is it working / is it progressing (documents processed per scope, code graph, health)
make watch                         # the same screen, refreshing until Ctrl-C
make down / make nuke              # stop (keep data) / remove containers, volumes and the k8s namespace
```

No Confluence/Backstage to test against? `make test-env` starts mock Confluence and Backstage serving
`test/fixtures` and mounts `test/docs`; the example `config/scopes.yaml` already points at them and at public
GitHub repos, so `make sync && make smoke ARGS=--live` proves isolation end to end (on Kubernetes: `kubectl apply -k test`). With an NVIDIA GPU add `EXTRA="-f docker/compose.gpu.yaml"`. Re-run `make gen`
whenever `scopes.yaml` changes.

## Kubernetes (k3s, OrbStack, any cluster)

Same images and service names as compose; scopes become per-scope Deployments/StatefulSets and a nightly indexer
CronJob. `k8s/base/` is hand-written, `k8s/generated/` is produced from `config/` by the generator
(gitignored: it contains the Secret).

```sh
docker compose build                       # gateway, ingest, codegraph images (OrbStack shares them with k8s;
                                           # elsewhere: docker save | k3s ctr images import, or push to a registry)
python3 scripts/gen-scopes.py k8s          # k8s/generated/: scope-*.yaml, Secret, ConfigMaps
kubectl apply -k k8s
kubectl -n context-stack get pods -w
kubectl -n context-stack create job --from=cronjob/indexer-public indexer-public-now    # first code-graph index
kubectl -n context-stack port-forward svc/ingest 8080:8080 &   # then: make sync
```

Host or external models: set `LLM_BASE_URL` etc. in `config/stack.env` (pods on OrbStack reach the host as
`host.docker.internal`) and `kubectl -n context-stack scale deploy/ollama --replicas=0`. Details in
[k8s/README.md](k8s/README.md).

## The demo

`scripts/demo.sh up` brings the whole stack up against fake organisation data (mock Confluence, Backstage and Jama,
public repositories), `connect --as alice` hooks Claude Code or Cursor up as one of four demo users, `activity` shows
what the stack does while the agent works, `down` removes it all. Storyline and prompts: [docs/DEMO.md](docs/DEMO.md).

## Wiring agents (Claude Code, Cursor, ...)

One URL per developer, behind SSO: `https://context.internal/mcp` → the gateway. See
**[docs/CONNECT.md](docs/CONNECT.md)**, including *how agents know when to recall and retain* (server
instructions + prompts, nothing installed on the client; a Stop-hook example if it must be enforced).
Engine ports (8888, 3000, per-scope 9621/8045) are admin-only.

## Keeping it fresh

| Source | Trigger | Path |
|---|---|---|
| Confluence | nightly cron (`INGEST_SCHEDULE_CRON`); optionally the source calls `POST /webhook/confluence` | version-diff per page, deletes reconciled |
| Backstage | nightly cron | entity + relations rendered as text |
| Repo docs | nightly cron (`git` plugin clones and content-hashes) | changed files only |
| Code | Sourcebot syncs on its own schedule; `indexer-<scope>` CronJob (nightly by default) checks upstream and skips repos whose HEAD has not moved | pull model: nothing pushes into the stack |

## Verified versions

All image tags are pinned; the gateway and ingest code were checked against these exact versions (2026-09):

| Component | Version | Verified |
|---|---|---|
| LightRAG | `ghcr.io/hkuds/lightrag:v1.5.7` | `POST /documents/texts`, `POST /documents/paginated`, `DELETE /documents/delete_document` (async, refuses while busy), `POST /query` (`include_references`), `X-API-Key`, storage/env names, one `WORKSPACE` per instance |
| Hindsight | `ghcr.io/vectorize-io/hindsight:0.9.2` | `/v1/default/banks/{bank}/memories` (retain, auto-creates the bank), `/memories/recall` (`budget`, `max_tokens`), `/reflect`; API-key tenant extension; Ollama LLM + OpenAI-style embeddings |
| Sourcebot | `ghcr.io/sourcebot-dev/sourcebot:v5.1.10` | `POST /api/search` (`query`, `matches`, `isRegexEnabled`), `Authorization: Bearer <api key>`, `repo:` is always a regex, `or` keyword; runs as root, needs `DATABASE_URL`/`REDIS_URL` |
| CodeGraphContext | `codegraphcontext==0.6.13` (MIT) | `cgc mcp start` (stdio), `cgc index <path>`, `DEFAULT_DATABASE=falkordb-remote` + `FALKORDB_HOST/PORT`, tool names; `CGC_ALLOWED_ROOTS` |
| supergateway | 3.4.3 | stdio → streamable HTTP bridge |
| python `mcp` | `>=1.30,<2` | 2.x renamed FastMCP; 1.x `stateless_http`, request headers via `ctx.request_context.request` |
| FalkorDB / Postgres / Redis / Ollama | `v4.20.4` / `pgvector 0.8.2-pg17` / `7.4-alpine` / `0.33.3` | |
| mcp-atlassian | `ghcr.io/sooperset/mcp-atlassian:0.23.1` (MIT) | streamable-http `--stateless --read-only --enabled-tools confluence_search,confluence_get_page`; `confluence_search(query, limit, spaces_filter)`, `confluence_get_page(page_id, include_metadata, convert_to_markdown)` → `{metadata, content}`; unauthenticated requests need `ALLOW_GLOBAL_CRED_FALLBACK=true` |
| Kubernetes | OrbStack k3s-based `v1.35.6+orb1` | `kubectl apply -k test`: all pods ready, sync + indexer CronJob + gateway verified; same manifests target any k3s |

Notes that shaped the code:

- LightRAG keeps only the basename of a document's `file_source`, so source ids are stored with `/` encoded as `|`
  (`ingest/ingest/lightrag.py`); the gateway decodes them in `query_docs` references.
- LightRAG has no update: the ingest collects a batch per scope, deletes changed/removed documents while the
  pipeline is idle, then inserts everything in one go and only then commits its state file.
- The gateway proxies only read-only CodeGraphContext tools (`CODEGRAPH_TOOLS` in `gateway/server.py`).
- Sourcebot's licence is FSL-1.1 (source-available). Everything else is MIT / Apache / CDDL.
- `gpt-oss:20b` needs ~16 GB RAM on CPU; swap `LLM_MODEL` for something smaller for a laptop pilot.

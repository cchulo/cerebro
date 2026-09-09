# agent-context-stack

Self-hosted context stack for AI agents:

| Layer | Engine | Purpose |
|---|---|---|
| Gateway | `./mcp/gateway` (this repo) | The one MCP endpoint agents use; enforces per-user access |
| Memory | [Hindsight](https://github.com/vectorize-io/hindsight) | What happened before: conversations, agent runs, outcomes (banks per user/team) |
| Documents | [LightRAG](https://github.com/HKUDS/LightRAG), **one instance per scope** | What the docs say: Confluence, Backstage, repo docs, ADRs |
| Code search | [Sourcebot](https://github.com/sourcebot-dev/sourcebot) | Exact / symbol search across remote repos, repo-filtered per user |
| Code graph | [CodeGraphContext](https://github.com/CodeGraphContext/CodeGraphContext) + FalkorDB, **one pair per scope** | Call graph, blast radius |
| Ingest | `./ingest` (this repo) | Syncs Confluence, Backstage and Git docs into the right scope |
| Shared | Postgres + pgvector, Ollama, Redis | One DB, one local model endpoint |

**Access control is built in**: `config/scopes.yaml` maps IdP groups → scopes → Confluence spaces + repos. Each scope
is an isolated docs graph and code graph; the gateway only queries the scopes the caller belongs to. Read
[docs/ACCESS-CONTROL.md](docs/ACCESS-CONTROL.md) before adding anything sensitive.

Everything runs locally; the only outbound traffic is to your code host and Confluence/Backstage.

## Layout

```
compose.yaml            shared services + Hindsight + Sourcebot + ingest + gateway
compose.scopes.yaml     GENERATED per-scope LightRAG / FalkorDB / CodeGraph / indexer services
compose.gpu.yaml        NVIDIA override for Ollama
config/scopes.yaml      access scopes: groups -> spaces + repos (edit this, then regenerate)
config/proxy/           example SSO reverse-proxy config
scripts/gen-scopes.py   regenerates compose.scopes.yaml and quadlet/scope-* from scopes.yaml
quadlet/                Podman Quadlet units (systemd) — same stack, no compose
config/postgres/        creates hindsight / lightrag / sourcebot DBs + pgvector
config/sourcebot/       which repos Sourcebot indexes
ingest/                 FastAPI service: scheduled + webhook sync into LightRAG
mcp/gateway/            identity-aware MCP gateway (docs, code, memory tools)
mcp/codegraph-mcp/      CodeGraphContext image with HTTP MCP bridge (used per scope)
docs/CONNECT.md         how to connect Claude Code / Cursor
docs/ACCESS-CONTROL.md  the scope model and its limits
index/index-repo.sh     clone + CodeGraphContext (+ optional SCIP) index for one or all repos
.github/workflows/      example push-triggered re-ingest / re-index
scripts/pull-models.sh  pulls the Ollama models
```

## Quick start (Docker / Podman compose)

```sh
cp .env.example .env               # edit secrets
edit config/scopes.yaml            # groups -> spaces + repos
python3 scripts/gen-scopes.py compose > compose.scopes.yaml
export COMPOSE_FILE=compose.yaml:compose.scopes.yaml
docker compose up -d postgres ollama
./scripts/pull-models.sh           # gpt-oss:20b (~13 GB) + bge-m3
docker compose up -d
docker compose --profile jobs run --rm indexer-public     # first code-graph index, one per scope
curl -X POST localhost:8080/sync/all -H "X-Ingest-Secret: $INGEST_WEBHOOK_SECRET"
```

With an NVIDIA GPU add `:compose.gpu.yaml` to `COMPOSE_FILE`. Re-run `gen-scopes.py` whenever `scopes.yaml` changes.

## Podman Quadlet (rootless systemd)

```sh
git clone <this repo> ~/agent-context-stack && cd ~/agent-context-stack
cp .env.example .env && edit .env
podman build -t localhost/stack-ingest:latest ./ingest
podman build -t localhost/stack-gateway:latest ./mcp/gateway
podman build -t localhost/stack-codegraph-mcp:latest ./mcp/codegraph-mcp
python3 scripts/gen-scopes.py quadlet      # writes quadlet/scope-*
mkdir -p ~/.config/containers/systemd
cp quadlet/* ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user start postgres ollama
podman exec ollama ollama pull gpt-oss:20b && podman exec ollama ollama pull bge-m3
systemctl --user start hindsight sourcebot ingest gateway scope-*-lightrag scope-*-falkordb scope-*-codegraph
systemctl --user enable --now scope-*-indexer.timer
loginctl enable-linger $USER          # keep running after logout
```

Units reference `%h/agent-context-stack/...` for config files, so keep the checkout at that path or edit the paths.

## Wiring agents (Claude Code, Cursor, ...)

One URL per developer, behind SSO: `https://context.internal/mcp` → the gateway. See
**[docs/CONNECT.md](docs/CONNECT.md)**. Engine ports (8888, 3000, per-scope 9621/8045) are admin-only.

## Keeping it fresh

| Source | Trigger | Path |
|---|---|---|
| Confluence | nightly cron (`INGEST_SCHEDULE_CRON`) or `POST /webhook/confluence` | version-diff per page, deletes reconciled |
| Backstage | nightly or `POST /webhook/backstage` | entity + relations rendered as text |
| Repo docs | push to main → `POST /webhook/git` | content-hash per file |
| Code | push to main → `indexer-<scope> <repo>`; nightly per-scope indexer | Sourcebot syncs on its own schedule |

## Things to verify on first run

- `cgc mcp start` is the assumed CodeGraphContext MCP subcommand; check `docker compose run --rm codegraph-public cgc --help`.
- Sourcebot search API path/payload (`POST /api/search`) and Hindsight REST paths used by the gateway: confirm against the versions you pull.
- The gateway trusts `X-Forwarded-*` headers: it must only be reachable through the SSO proxy.
- LightRAG endpoint paths used by `ingest/ingest/lightrag.py` (`/documents/text`, `/documents/delete_document`) — confirm on `http://localhost:9621/docs` for the image version you pull.
- Sourcebot's licence is FSL-1.1 (source-available). Everything else is MIT / Apache / CDDL.
- `gpt-oss:20b` needs ~16 GB RAM on CPU; swap `LLM_MODEL` for something smaller for a laptop pilot.
- Pin image tags before production; `latest` is used here for the pilot.

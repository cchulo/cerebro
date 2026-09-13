# cerebro

Self-hosted context for AI coding agents: one MCP endpoint in front of your documentation, your code and the
agents' own memory, with access control that lives in the index rather than in a result filter. Everything behind
the gateway is an adapter chosen in one file, `cerebro.yaml`: the document index, the code intelligence engine, the
memory store, who mints identities, and what provisions the workloads (Docker Compose or Kubernetes).

| Layer | Default engine | What it answers | Unit(s) it runs |
|---|---|---|---|
| Gateway | `cerebro/gateway` (this repo, FastMCP) | the tools agents call; identity, policy, fan-out | `gateway`, port 8090 |
| Documents | [LightRAG](https://github.com/HKUDS/LightRAG) `v1.5.7`, one server per scope | "what do the docs say" (`query_docs`, live fallback) | `docs-<scope>`, port 9621 |
| Code | [TokenSave](https://github.com/aovestdipaperino/tokensave) `7.12.1` behind the cerebro bridge, plus ripgrep | text search and code graph (`search_code`, `code_tool`) | `code-<scope>` or `code-<scope>-<repo>`, port 8045, one `index-<unit>` job each |
| Memory | [Hindsight](https://github.com/vectorize-io/hindsight) `0.9.2` | what agents learned by doing (`recall`, `retain`, `reflect`), banks per user and team | `memory`, port 8888 |
| Identity | mode `none` (one user), `static` (tests), `builtin` ([Keycloak](https://www.keycloak.org) `26.7.3`), `external` (your IdP), legacy trusted headers | who is calling and which token scopes it holds | `auth`, port 8080 (builtin only) |
| Ingest | `cerebro/ingest` (this repo) | syncs document sources into the per-scope indexes through `plugins/` | `ingest`, port 8080 |
| Provisioning | `compose` or `kubernetes` | renders and drives every unit above from `cerebro.yaml` | `postgres` (pgvector), `ollama` when used, `mcp-<plugin>` upstreams |

A second `DocumentIndex` implementation, `pgvector`, keeps chunks and embeddings in the shared Postgres with no LLM
extraction: cheap ingest, retrieved passages instead of a synthesised answer, one line of config to switch
(`engines.docs.type: pgvector`). Verified against a real Postgres in tests.

## What the model sees, and where data can leave

Nothing here calls a cloud service by itself. The `inference:` block of `cerebro.yaml` is the only place document and
memory text is sent for processing; point it at Ollama or at an OpenAI-compatible endpoint your organisation controls.

| Component | Sends text to the inference backend | Other network activity |
|---|---|---|
| LightRAG (`docs-<scope>`) | every ingested document at extraction; the question and retrieved context at `query_docs` | none |
| Hindsight (`memory`) | what agents `retain`; recalled memories during `reflect`; embeddings | one-time download of the `local` reranker (a small cross-encoder) at first start; `engines.memory.options.reranker: rrf` avoids it |
| TokenSave (`code-*`) | nothing; no model is involved | its token-counter upload is switched off in the image (`upload_enabled = false`) and its GitHub update check with `TOKENSAVE_UPDATE_CHECK=off` |
| Keycloak (`auth`) | nothing | none |
| Ingest, index jobs | nothing | read Confluence, Backstage, Jama and your code host; never write to them |

One-time downloads: Ollama pulls the models you name from ollama.com; the code-unit image fetches the TokenSave
release tarball at image build (SHA256-checked); Hindsight fetches its reranker as above. Ingest and gateway images
are built from this repository.

## Quick start: one person, one machine

Identity mode `none` means no tokens and no login. Inside a container that mode binds the gateway to the
container's own loopback, which the host cannot reach, so the compose path uses `allow_remote` with a static token
(verified against the built image; see [docs/IDENTITY.md](docs/IDENTITY.md)).

```sh
uv venv .venv && uv pip install -e '.[all]'         # Python 3.12+
cp cerebro.example.yaml cerebro.yaml
cp secrets.env.example secrets.env                 # fill POSTGRES_PASSWORD, LIGHTRAG_API_KEY, HINDSIGHT_API_KEY, CEREBRO_TOKEN
```

In `cerebro.yaml` set `identity.allow_remote: true`, `gateway.host: 0.0.0.0` (the port is then published on every
interface of the machine; the token is what guards it), add `CEREBRO_TOKEN` to `secrets.keys`, trim `scopes:` to what
you have, and turn off the Confluence upstream if you have no Confluence (`sources: { confluence: { live: { enabled: false } } }`).

```sh
.venv/bin/cerebro validate                          # the resolved scopes and code units
.venv/bin/cerebro provision plan                    # every unit and job, with the secrets each one needs
.venv/bin/cerebro provision up                      # renders deploy/generated/compose.yaml, builds, starts, waits
docker compose -p cerebro -f deploy/generated/compose.yaml exec ollama ollama pull gpt-oss:20b
docker compose -p cerebro -f deploy/generated/compose.yaml exec ollama ollama pull bge-m3
.venv/bin/cerebro provision job index-code-public --wait          # clone + index the scope's repositories
docker compose -p cerebro -f deploy/generated/compose.yaml exec ingest cerebro ingest sync   # documents
claude mcp add --transport http cerebro http://127.0.0.1:8090/mcp --header "Authorization: Bearer $CEREBRO_TOKEN"
```

`gpt-oss:20b` needs about 16 GB of RAM on CPU; pick a smaller chat model in `inference.llm.model` for a laptop.
An Ollama already running on the host: `LLM_BASE_URL=http://host.docker.internal:11434 EMBED_BASE_URL=... cerebro provision up`
drops the `ollama` unit ([docs/SETUP.md](docs/SETUP.md)).

## Quick start: a team, builtin Keycloak

Same install. In `cerebro.yaml`: `identity.mode: builtin`, `identity.server: { type: keycloak, realm: cerebro, public_url: https://auth.example.org }`,
`identity.issuer: https://auth.example.org/realms/cerebro` (the gateway does not derive it yet), `identity.users:`,
`gateway.public_url: https://context.example.org/mcp`, and `CEREBRO_AUTH_ADMIN_USER`, `CEREBRO_AUTH_ADMIN_PASSWORD`
in `secrets.keys` and `secrets.env`. Then `cerebro provision up`, seed the realm (users, groups, the `cerebro-mcp`
public client, token scopes with audience mappers) as described in [docs/IDENTITY.md](docs/IDENTITY.md), put your
TLS reverse proxy or Ingress in front of `gateway` and `auth`, and hand every developer the one URL. Their MCP client
discovers the authorization server from the gateway's RFC 9728 metadata and logs in with PKCE
([docs/CONNECT.md](docs/CONNECT.md)). Verified so far: unit tests against a mocked Keycloak admin API; not yet run
against a live Keycloak in v2.

## Layout

```
cerebro.example.yaml        the one config; copy to cerebro.yaml (gitignored)
secrets.env.example         every secret name the stack may need; copy to secrets.env (gitignored)
cerebro/core/               contracts, config schema, Principal/Grants, units, registry; imports no engine
cerebro/gateway/            the MCP server: tools, identity middleware, live fallback
cerebro/ingest/             sync engine, webhook/schedule service, `cerebro ingest` CLI
cerebro/bridge/             stdio-to-HTTP bridge that runs inside code-unit images (grep, manifest, /mcp)
cerebro/provision/          plan(), the kopf idle operator; cerebro/provision_cli.py drives targets
cerebro/adapters/<kind>/    identity/ auth/ policy/ docs/ code/ memory/ inference/ provision/ state/
cerebro/sdk/                what plugins/*.py import
plugins/                    knowledge sources: confluence, backstage, git, files, jama
images/                     Dockerfiles cerebro builds: gateway, ingest, code-unit
deploy/                     postgres/init.sql; deploy/generated/ is rendered and gitignored
tests/contracts/            the harness every adapter must pass; tests/<component>/ next to it
docs/                       ARCHITECTURE, SETUP, ACCESS-CONTROL, IDENTITY, CONNECT, PLUGINS, ENGINES, DESIGN-V2
```

Docs: [ARCHITECTURE](docs/ARCHITECTURE.md) (diagrams), [SETUP](docs/SETUP.md) (config walkthrough, compose,
Kubernetes, indexing, syncing), [ACCESS-CONTROL](docs/ACCESS-CONTROL.md) (the scope model and what the gateway
checks), [IDENTITY](docs/IDENTITY.md) (the identity modes), [CONNECT](docs/CONNECT.md) (Claude Code, Cursor),
[PLUGINS](docs/PLUGINS.md) (adding a source), [ENGINES](docs/ENGINES.md) (contracts, licences),
[DESIGN-V2](docs/DESIGN-V2.md) (why it is shaped this way).

**v1**: the proof of concept (LightRAG + Sourcebot + CodeGraphContext + Hindsight behind an SSO proxy, verified end to
end with a demo) is frozen on the [`v1` branch](../../tree/v1). It is not maintained; `main` is v2.

## Pinned versions and what was verified

| Component | Pinned | Verified in v2 |
|---|---|---|
| LightRAG | `ghcr.io/hkuds/lightrag:v1.5.7` | unit tests against mocks of the same calls v1 verified live (`/documents/texts`, `/documents/paginated`, `/documents/delete_document`, `/query`, `X-API-Key`) |
| Hindsight | `ghcr.io/vectorize-io/hindsight:0.9.2` | unit tests against mocks of the v1 wire calls (`/v1/{tenant}/banks/{bank}/memories`, `/recall`, `/reflect`) |
| Keycloak | `quay.io/keycloak/keycloak:26.7.3` | unit tests against a stateful mock of the admin REST API; RFC 8707 and CIMD status read from its documentation ([docs/IDENTITY.md](docs/IDENTITY.md)) |
| TokenSave | `7.12.1` in `cerebro/code-unit` (built here) | real binary: tool inventory, `graph_root`/`graph_branch` behaviour, branch tracking, the served empty root; the bridge and indexer against real git repositories |
| Postgres | `pgvector/pgvector:pg16` | rendered manifests only |
| Ollama | `ollama/ollama:0.33.3` | rendered manifests; native and OpenAI-compatible adapters against mocks |
| mcp-atlassian | `ghcr.io/sooperset/mcp-atlassian:0.23.1` | rendered as `mcp-confluence`; tool names as v1 verified live |
| compose scheduler | `docker:27-cli` (busybox crond) | rendered manifests only |
| python `mcp` | `>=1.10,<2` | gateway and bridge tests over streamable HTTP |
| Gateway image, identity mode `none` and `allow_remote` | `images/gateway/Dockerfile` | built and run: loopback bind inside the container, 401 without the token, MCP initialize with it |

`pytest` runs the whole suite (270 tests); the tests that need the TokenSave binary skip without it.

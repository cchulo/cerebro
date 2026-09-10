# Setting up the stack from scratch

This walks through everything once, in order: what is in the repo, what each configuration file is for, and the
two ways to run it (Docker Compose on one machine, Kubernetes/k3s for anything you want to scale). Read
[ACCESS-CONTROL.md](ACCESS-CONTROL.md) alongside it before you point it at real data.

## 1. What you are deploying

One gateway that AI coding agents talk to over MCP, and behind it three kinds of knowledge, each held by a
different engine, isolated per **scope** (an IdP group):

| Layer | Engine | Fed by |
|---|---|---|
| Agent memory (what happened before) | Hindsight | agents calling `retain`; nothing else |
| Documents (what the docs say) | LightRAG, one instance per scope | the ingest service through plugins (`plugins/`: Confluence, Backstage, git docs, files, yours) |
| Code search | Sourcebot, one instance | Sourcebot syncs the repos itself |
| Code graph (callers, blast radius) | CodeGraphContext + FalkorDB, one pair per scope | the per-scope indexer job |
| Live fallback (when the index misses) | each plugin's live part, e.g. Confluence via the mcp-atlassian upstream it declares (`mcp-confluence`) | the same credentials; confined per scope by the gateway |
| Shared | Postgres + pgvector, Redis, an inference backend (Ollama or an org-approved endpoint) | |

The only text that ever leaves the stack goes to the inference backend you configure. See "What the model sees"
in the [README](../README.md).

## 2. Repository layout

```
docker/                   Compose files: compose.yaml (shared services), compose.scopes.yaml (GENERATED, gitignored),
                          compose.host-ollama.yaml, compose.gpu.yaml, compose.test.yaml (overrides). Paths inside are
                          relative to docker/; the Makefile passes the right -f flags
config/                   everything you edit — see section 3
plugins/                  one file per source (ingest part, live fallback, MCP upstream image), auto-discovered — docs/PLUGINS.md
sdk/                      the stack_plugins framework those files use (installed in the ingest and gateway images)
k8s/                      Kubernetes manifests: base/ hand-written, generated/ (make gen) — see section 6
mcp/gateway/              the identity-aware MCP gateway (Python, FastMCP)
mcp/codegraph-mcp/        CodeGraphContext image with an HTTP MCP bridge; also runs the indexer job
ingest/                   the ingest service and its source adapters
index/index-repo.sh       clone + index one or all repos of a scope
scripts/gen-scopes.py     generator: config -> docker/compose.scopes.yaml, k8s/generated/
scripts/smoke-test.py     access-control checks against the gateway
test/                     mock services, fixtures, and a Kubernetes overlay for the test environment
docs/                     this file, ACCESS-CONTROL.md (the scope model), CONNECT.md (client setup),
                          ENGINES.md (what each engine is for and is not), ARCHITECTURE.md (diagrams)
```

## 3. The `config/` directory

Everything you are expected to edit lives here. Nothing else in the repo needs changes for a normal deployment.

### `config/stack.env` (copy from `config/stack.env.example`) — secrets and the inference backend

The single environment file for the whole stack. Compose reads it for variable interpolation
(`COMPOSE_ENV_FILES=config/stack.env`, which the Makefile exports), and the Kubernetes generator turns it into the
`stack-env` Secret. It is gitignored. Sections:

| Block | Keys | Notes |
|---|---|---|
| Shared | `POSTGRES_USER`, `POSTGRES_PASSWORD` | one Postgres, a database per engine |
| Inference backend | `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `EMBED_*`, `HINDSIGHT_EMBED_BASE_URL` | `ollama` (local; URL is the server root) or `openai` (any OpenAI-compatible endpoint your org controls; URL is the `/v1` base). This is the only place document and memory text is sent. |
| Hindsight | `HINDSIGHT_API_KEY`, `HINDSIGHT_CP_ACCESS_KEY`, `HINDSIGHT_RERANKER` | API key every call must carry (only the gateway has it); UI login key; `local` reranker (one-time model download) or `rrf` (none) |
| LightRAG | `LIGHTRAG_API_KEY` | shared by all scope instances; only ingest and gateway hold it |
| Sourcebot | `SOURCEBOT_AUTH_SECRET`, `SOURCEBOT_ENCRYPTION_KEY`, `SOURCEBOT_AUTH_URL`, `GITHUB_TOKEN`, `SOURCEBOT_API_KEY` (optional) | generate the two secrets with `openssl rand -base64 33` / `24`; the gateway searches anonymously (Sourcebot runs with `FORCE_ENABLE_ANONYMOUS_ACCESS` on its private port), so no key is needed |
| Ingest + live fallback | `CONFLUENCE_*`, `BACKSTAGE_*`, `GIT_DOC_GLOBS`, `INGEST_WEBHOOK_SECRET`, `INGEST_SCHEDULE_CRON` | credentials for the shipped adapters and for mcp-atlassian; leave a block empty and that adapter reports "not configured" |

Generate real secrets before the first start; every `change-me` value is a placeholder.

### `config/scopes.yaml` — who may see what

The access model, and the one place where document access and code access are defined together. Each **scope**
lists the IdP `groups` allowed to read it, `code.repos` (code search, code graph, and the docs inside those repos
via the `git` plugin) and `docs`, a map of adapter name → what of that source belongs to the scope. A repo or
Confluence space may appear in exactly one scope; the ingest refuses to start otherwise.

```yaml
scopes:
  payments:
    groups: [payments-team, platform-leads]
    code:
      repos: [https://github.com/your-org/payments.git]
    docs:
      confluence: { spaces: [PAY] }
      git: {}
```

The `identity` block names the headers the SSO proxy sets; `sources:` carries non-secret adapter options; `live:`
enables query-time fallbacks (docs/SOURCES.md). The shipped file is a working example against public GitHub repos and the mock
Confluence/Backstage in `test/`, so the test environment runs without edits. Whenever you change this file:
`make gen`, then `make up` (compose) or `kubectl apply -k k8s`.

### `config/postgres/init.sql` — database bootstrap

Runs once, on the first start of the Postgres container (mounted into `docker-entrypoint-initdb.d/`; on Kubernetes
it is a ConfigMap). It creates the three databases (`hindsight`, `lightrag`, `sourcebot`) and enables the
`vector` extension where needed. Every engine then manages its own schema. If you point the stack at an existing
Postgres instead, run this file by hand once.

### `config/sourcebot/config.json` — which repositories Sourcebot syncs

Sourcebot's own configuration file (its schema URL is in the file). `connections` says where to fetch code from;
the shipped file lists four public GitHub repos with no token. `config.private-org.example.json` shows the usual
production form: a whole GitHub org with a token taken from the `GITHUB_TOKEN` environment variable and archived
repos and forks excluded. Keep this list and the `repos` in `scopes.yaml` in agreement: Sourcebot indexes what this
file says, the gateway only lets a user search repos of their scopes, and anything indexed here but listed in no
scope is unreachable through the gateway.

Branches: by default only each repo's default branch (HEAD) is indexed. Add `revisions` to a connection to index
more, with globs (`"revisions": { "branches": ["main", "release/*"], "tags": ["v2.*.*"] }`; HEAD is always
included; at most 64 branches and 64 tags per repo). Queries then take a `rev:` filter (`rev:release/2.3 handlePayment`),
which works through the gateway's `search_code` unchanged. Index size and sync time grow with every branch, so avoid
`["**"]` on busy repos. This is Sourcebot only: the code graph and the git docs adapter read the default branch.

### `config/proxy/Caddyfile.example` — the SSO front door

The gateway trusts two headers, `X-Forwarded-User` and `X-Forwarded-Groups`, and refuses requests without them. It
must therefore only be reachable through a reverse proxy that authenticates the user against your IdP and sets
those headers. The example is Caddy in front of oauth2-proxy (OIDC), mapping oauth2-proxy's header names to the
gateway's. Any OIDC-capable proxy (Authelia, Pomerium, Traefik forward-auth, an Ingress with an auth annotation)
works the same way. This file is an example to adapt, not something the stack starts for you.

## 4. Prerequisites

- Docker with Compose v2 (Docker Desktop, OrbStack, or Docker Engine), or a Kubernetes cluster for section 6.
- Python 3.10+ for the generator and the smoke test: `pip install -r scripts/requirements.txt` (PyYAML and the
  MCP client). Use a virtualenv if your system Python is externally managed.
- An inference backend: either Ollama (in the stack, on the host, or on a GPU box) with a chat model and the
  `bge-m3` embedding model, or an OpenAI-compatible endpoint your organisation approves.
- RAM: shared services ~4 GB, plus 1–2 GB per scope, plus the model if Ollama runs here (`gpt-oss:20b` ~16 GB).

## 5. Docker Compose (one machine)

```sh
git clone <repo> && cd <repo>
pip install -r scripts/requirements.txt
cp config/stack.env.example config/stack.env      # then edit: secrets, inference backend, adapter credentials
edit config/scopes.yaml config/sourcebot/config.json
make gen                                          # docker/compose.scopes.yaml + k8s/generated/
make up                                           # everything, Ollama included
make models                                       # pulls LLM_MODEL and EMBED_MODEL into the Ollama container
```

Ollama already on the host (Apple silicon, a GPU workstation): set `LLM_BASE_URL`, `EMBED_BASE_URL` and
`HINDSIGHT_EMBED_BASE_URL` to `http://host.docker.internal:11434` (`.../v1` for the last one) in `stack.env`, pull
the models with `ollama pull` on the host, and use `make up EXTRA="-f docker/compose.host-ollama.yaml"`. NVIDIA inside
Docker: `EXTRA="-f docker/compose.gpu.yaml"`.

Then, in this order:

1. **Code graph**: `make index` runs one indexer job per scope (clone + `cgc index` into that scope's FalkorDB);
   `scripts/up.sh --index --sync` does this and the next step for you.
2. **Documents**: `make sync` asks the ingest to run every plugin for every scope. LightRAG processes the
   batch in the background with the model; `docker compose logs -f lightrag-<scope>` shows progress.
3. **Check isolation**: `make smoke` (identity headers forged directly against the gateway) and, once documents are
   processed, `make smoke ARGS=--live`.
4. **Put the SSO proxy in front** of `127.0.0.1:8090` (section 3, proxy). Every published port is bound to
   loopback; nothing else should be reachable from the network. Sourcebot's own UI (port 3000) runs with anonymous
   access for the gateway's benefit; keep it off the network too.
5. **Connect agents**: one URL per developer, see [CONNECT.md](CONNECT.md). Agents learn the routing and the
   recall-first / retain-last rule from the server itself (MCP instructions + prompts); CONNECT.md explains the
   limits and how to enforce `retain` with a client hook.

No Confluence or Backstage to test with yet? `make test-env` adds mock services that serve `test/fixtures` and
mounts `test/docs`; the shipped `scopes.yaml` already targets them, so the steps above work unchanged.

Keeping it fresh is a pull model: the ingest runs on `INGEST_SCHEDULE_CRON` (incremental, so hourly is cheap), the
indexer jobs run on their CronJob schedule and skip repositories whose upstream HEAD has not moved, and Sourcebot
syncs on its own schedule. Nothing outside the stack needs to know where it runs; `POST /webhook/<adapter>` exists
only for sources that can call in (Confluence Cloud, JAMA events) and is optional.

## 6. Kubernetes (k3s, OrbStack, any cluster)

Same images, same service names, so the gateway and ingest are unchanged. `k8s/base/` is hand-written;
`k8s/generated/` is produced from `config/` by `make gen` (or `python3 scripts/gen-scopes.py k8s`) and contains the
Secret, so it is gitignored.

```sh
cp config/stack.env.example config/stack.env && edit
docker compose build                              # gateway, ingest, codegraph images
make gen
kubectl apply -k k8s                              # or: kubectl apply -k test   (stack + mock services)
kubectl -n context-stack get pods -w
```

Getting the images to the cluster: OrbStack shares the Docker image store, nothing to do. On a plain k3s host,
`docker save agent-context-stack-gateway:latest | sudo k3s ctr images import -` (same for `-ingest` and
`-codegraph-public`), or push to a registry and change `images:` in `k8s/kustomization.yaml`. Models: the
in-cluster Ollama is `deploy/ollama`; for a host or external backend set the URLs in `stack.env` and scale it to 0.

Then the same order as compose, through port-forwards:

```sh
kubectl -n context-stack create job --from=cronjob/indexer-public indexer-public-now   # per scope
kubectl -n context-stack port-forward svc/ingest 8080:8080 &  && make sync
kubectl -n context-stack port-forward svc/gateway 8090:8090 & && make smoke
```

Every Service is ClusterIP. Add an Ingress with OIDC auth in front of `svc/gateway` in your own overlay; never
expose the engine Services. Multi-node clusters: the per-scope `repos-<scope>` claim is shared by the codegraph pod
and its indexer job, so use a ReadWriteMany StorageClass or pin both to one node. More in
[../k8s/README.md](../k8s/README.md).

## 7. Adding a document source

One file in `plugins/` with `PLUGIN = Plugin(name=..., source=..., live=..., mcp=...)`: the ingest part yields
`Document(key, version, text, title)`, the optional live part answers `search`/`fetch` under the caller's scopes, and
the optional `McpUpstream` names an MCP server image that `make gen` runs as `mcp-<name>`. Discovered at startup, no
registration, no rebuild. Secrets from `stack.env`, options from `sources:`/`live:`, what-belongs-where from a
scope's `docs:`. `make source-check SCOPE=... SOURCE=...` lists what the ingest part yields without LightRAG.
Full guide: [PLUGINS.md](PLUGINS.md); the freshness model: [SOURCES.md](SOURCES.md).

## 8. Lifecycle scripts

`scripts/up.sh` and `scripts/down.sh` wrap everything above for both targets; the Makefile targets call them.

Everything the stack creates is labelled: `context-stack.io/project=agent-context-stack` on every container, volume
and network (plus `context-stack.io/scope=<name>` per scope and `context-stack.io/plugin=<name>` on generated MCP
upstreams), `app.kubernetes.io/part-of=context-stack` on every Kubernetes object including PersistentVolumeClaims.
`down.sh` selects by those labels, so a scope removed from `scopes.yaml` is still cleaned up, and nothing without the
label (other projects, hand-made volumes) is ever touched.

| Command | Effect |
|---|---|
| `scripts/up.sh` | regenerate, build images, `docker compose up`, wait for every service; every phase prints a timestamped line |
| `scripts/up.sh --sync --wait` | ... then keep showing documents processed per scope until all are done |
| `scripts/status.sh` / `--k8s` | is it working, is it progressing (section 9) |
| `scripts/watch.sh` / `--k8s` / `--interval N` | the status screen refreshing every 5 s until Ctrl-C |
| `scripts/activity.py` / `--mode k8s` / `--all` | live trail: gateway tool calls (white) and engine activity (green) |
| `scripts/demo.sh up \| ready \| watch \| activity \| connect \| add-page \| smoke \| down` | the demo against fake data end to end ([DEMO.md](DEMO.md)) |
| `scripts/up.sh --test --host-ollama --index --sync` | test environment with host Ollama, then index every scope's code and sync all sources |
| `scripts/up.sh --k8s [--test]` | `kubectl apply -k k8s` (or `test`) and wait for pods |
| `scripts/down.sh` | remove containers and networks, keep data volumes |
| `scripts/down.sh --volumes` | ... and delete all data (Postgres, LightRAG, FalkorDB, repos, ingest state) |
| `scripts/down.sh --k8s [--volumes]` | delete the Kubernetes workloads (and PVCs + namespace with `--volumes`) |
| `scripts/down.sh --all-targets --volumes` (= `make nuke`) | complete teardown of both compose and Kubernetes; images kept |
| `scripts/down.sh --nuke` | the above plus every built and pulled image |

## 9. Checking progress (it is not stuck)

The slow part of any bring-up is LightRAG extracting documents with the model; the containers are up long before
that finishes, and nothing prints while it runs. Use these to see progress:

| Docker Compose | Kubernetes |
|---|---|
| `make status` (once) / `make watch` (every 5 s until Ctrl-C) | `make k8s-status` / `make k8s-watch` |
| `scripts/up.sh ... --sync --wait` keeps showing progress after starting | `scripts/up.sh --k8s ... --sync --wait` |
| `docker compose -f docker/compose.yaml -f docker/compose.scopes.yaml logs -f lightrag-<scope>` | `kubectl -n context-stack logs -f deploy/lightrag-<scope>` |
| `docker compose ... logs -f ingest` (what was listed and handed over) | `kubectl -n context-stack logs -f deploy/ingest` |
| `docker compose ... ps` | `kubectl -n context-stack get pods -w` |

`make smoke` narrates itself: every call is printed before it runs (white/dim) with its elapsed time after, results are
white or red, and while it runs the stack's own log activity (gateway requests, LightRAG queries and extraction,
mcp-confluence calls, Hindsight) streams in green, auto-detected from Compose or Kubernetes (`--activity`,
`--no-color`). A long pause with green lines is the model working; a long pause without any is worth a look.

`status` shows, per scope, `processed/total` documents with the pipeline's latest message, which repositories the
code graph has indexed, the last sync line and the health of memory and search. "processed" counts documents whose
extraction is finished; `query_docs` answers only from those. A `failed` count means the model endpoint was
unreachable during extraction (section 12).

## 10. Upgrading and changing things

| Change | Do |
|---|---|
| `scopes.yaml` (groups, repos, docs) | `make gen`, then `make up` / `kubectl apply -k k8s`; a moved repo or space needs `make index` and `make sync` again |
| `stack.env` | `make up` (compose recreates changed containers) / `make gen && kubectl apply -k k8s` |
| gateway or ingest code | `make build && make up` / `docker compose build` + `kubectl rollout restart deploy/<name>` |
| engine versions | tags are pinned in `docker/compose.yaml`, `scripts/gen-scopes.py` and `k8s/base/`; bump, re-verify the API notes in the README, redeploy |
| new scope | add it to `scopes.yaml`, `make gen`, deploy, `make index`, `make sync` — it costs one LightRAG + one FalkorDB/CodeGraphContext pair |
| removed scope | on Kubernetes also `kubectl -n context-stack delete all,pvc -l scope=<name>` |

## 11. Performance: what the model endpoint has to do

Nothing in the stack is slow by itself; the cost is LLM calls, and every LightRAG instance and Hindsight share the
one endpoint in `stack.env`. Ingest cost is almost entirely **output tokens**: prompt evaluation of a 1,200-token chunk
takes well under a second, generation runs at 80 tokens/s on an M-series laptop with `qwen3.6:35b-mlx`, so what
matters is how many tokens the model emits per chunk. Measured with LightRAG 1.5.7's real extraction prompt on one
1,200-token chunk:

| Setting | Output tokens | Wall time | Entities / relations found |
|---|---|---|---|
| model default (hidden reasoning on) | 6,560 (23,000 characters of thinking) | 60 s | 15 / 8 |
| `LIGHTRAG_LLM_THINK=false` (default here) | 675 | 7 s | 15 / 5 |

Thinking-capable models (qwen3.6, gpt-oss, deepseek-r1) reason at length before every extraction; LightRAG's own
guidance is "a non-thinking model (reasoning/thinking mode disabled) is strongly recommended to avoid slow, expensive
extraction". The switch is the Ollama binding's `think` option; for an OpenAI-compatible org endpoint use a model
served without reasoning, or one whose reasoning effort is set to minimum server-side.

What one operation costs, in LLM calls:

| Work | LLM calls | Notes |
|---|---|---|
| Ingesting one document | 1 extraction per ~1,200-token chunk (+1 more per chunk with gleaning), plus a summary call for an entity merged more than 8 times | a 5-page runbook ≈ 5–10 calls |
| `query_docs` (mix/hybrid/local/global) | 1 keyword extraction + 1 answer | `naive` skips the keyword step |
| `retain` | 1 extraction (async, the agent does not wait) + periodic consolidation | |
| `search_code`, `code_graph`, `live_search` | 0 | |

Queueing is the other enemy: a query waits behind every in-flight extraction call. The knobs, all in `stack.env`:

- `LIGHTRAG_LLM_THINK=false` (default): no hidden reasoning in LightRAG's calls. Also speeds up `query_docs` answers.
- `LIGHTRAG_MAX_OUTPUT_TOKENS=4096` (default): output cap per call; stops a chunk that sends the model into a loop.
- `LIGHTRAG_MAX_GLEANING=0` (default): one extraction pass per chunk instead of two.
- `OLLAMA_NUM_PARALLEL` (in-stack Ollama; for a host Ollama set it in its environment): requests served concurrently.
- `LIGHTRAG_MAX_ASYNC` (concurrent LLM calls per instance, default 2 here) and `LIGHTRAG_MAX_PARALLEL_INSERT`
  (documents processed at once, default 1): with N scopes ingesting, total in-flight calls = N × MAX_ASYNC; keep that
  at or below `OLLAMA_NUM_PARALLEL` or calls queue inside Ollama.
- The size of what you ingest: `git` globs, Confluence spaces, Jama projects. The example config indexes only
  READMEs from the public repos for this reason; widen it once the endpoint has capacity.
- An org endpoint with real throughput (`LLM_PROVIDER=openai`) removes the constraint entirely: extraction and
  queries no longer compete for one laptop GPU.

LightRAG caches extraction results per chunk text (`ENABLE_LLM_CACHE_FOR_EXTRACT`, on by default), so re-syncing a
document whose text did not change costs no LLM calls even though the ingest deletes and re-inserts it.
LightRAG's *query-answer* cache (`ENABLE_LLM_CACHE`) is switched off by the generator: it hands back the old answer
verbatim after a sync has added exactly the document the question was about (verified on 1.5.7), which defeats the
point of keeping the index fresh. Each `query_docs` therefore costs its two model calls every time.

Schedule ingest when nobody is querying (the crons default to 02:00/03:00). Expect the first sync of a large space to
take a while on a single local model regardless of settings: 1,000 pages ≈ 1,000–3,000 chunks ≈ 2–6 hours at 7 s each
with three calls in flight.

## 12. Troubleshooting

- `gateway` refuses every call with "missing X-Forwarded-User": the request did not come through the proxy (or the
  smoke test URL is wrong). This is by design.
- `search_code` returns 401: a stale `SOURCEBOT_API_KEY` in `stack.env` (keys live in Sourcebot's database and die
  with its volume). Leave it empty; the gateway searches anonymously.
- `query_docs` answers "no context": LightRAG has not finished processing; check `/documents/pipeline_status` via
  `docker compose logs lightrag-<scope>` or `kubectl logs deploy/lightrag-<scope>`.
- Ingest logs "not configured": that adapter's credentials block in `stack.env` is empty.
- Hindsight restarts once or twice on a cold cluster: it waits for Postgres now; older manifests crash-looped until
  Postgres was up.
- A scope's indexer job fails with permission errors on `/home/cgc/.codegraphcontext`: the volume was created by an
  older image; delete the `cgc-<scope>` volume/claim and re-run.

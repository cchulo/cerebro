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
| Documents (what the docs say) | LightRAG, one instance per scope | the ingest service (adapters: Confluence, Backstage, git docs, files, your own) |
| Code search | Sourcebot, one instance | Sourcebot syncs the repos itself |
| Code graph (callers, blast radius) | CodeGraphContext + FalkorDB, one pair per scope | the per-scope indexer job |
| Shared | Postgres + pgvector, Redis, an inference backend (Ollama or an org-approved endpoint) | |

The only text that ever leaves the stack goes to the inference backend you configure. See "What the model sees"
in the [README](../README.md).

## 2. Repository layout

```
compose.yaml              shared services + Hindsight + Sourcebot + ingest + gateway
compose.scopes.yaml       GENERATED per-scope services (make gen) — gitignored
compose.host-ollama.yaml  override: use an Ollama already running on the host
compose.gpu.yaml          override: NVIDIA GPU for the Ollama container
compose.test.yaml         override: mock Confluence + Backstage and fixture docs (no real systems needed)
config/                   everything you edit — see section 3
k8s/                      Kubernetes manifests: base/ hand-written, generated/ (make gen) — see section 6
mcp/gateway/              the identity-aware MCP gateway (Python, FastMCP)
mcp/codegraph-mcp/        CodeGraphContext image with an HTTP MCP bridge; also runs the indexer job
ingest/                   the ingest service and its source adapters
index/index-repo.sh       clone + index one or all repos of a scope
scripts/gen-scopes.py     generator: config -> compose.scopes.yaml, k8s/generated/
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
| Sourcebot | `SOURCEBOT_AUTH_SECRET`, `SOURCEBOT_ENCRYPTION_KEY`, `SOURCEBOT_AUTH_URL`, `GITHUB_TOKEN`, `SOURCEBOT_API_KEY` | generate the two secrets with `openssl rand -base64 33` / `24`; the API key is created in Sourcebot's UI after first start |
| Ingest | `CONFLUENCE_*`, `BACKSTAGE_*`, `GIT_DOC_GLOBS`, `INGEST_WEBHOOK_SECRET`, `INGEST_SCHEDULE_CRON` | credentials for the built-in adapters; leave a block empty and that adapter reports "not configured" |

Generate real secrets before the first start; every `change-me` value is a placeholder.

### `config/scopes.yaml` — who may see what

The access model. Each **scope** lists the IdP `groups` allowed to read it, the `repos` indexed into it (code
search + code graph, and the docs inside them via the `git` adapter), and `docs`, a map of adapter name → adapter
config for the documents indexed into it. A repo or Confluence space may appear in exactly one scope; the ingest
refuses to start otherwise.

```yaml
scopes:
  payments:
    groups: [payments-team, platform-leads]
    repos: [https://github.com/your-org/payments.git]
    docs:
      confluence: { spaces: [PAY] }
      git: {}
```

The `identity` block names the headers the SSO proxy sets, and `sources:` declares custom adapters
(`type: "my_pkg.jira:JiraSource"`). The shipped file is a working example against public GitHub repos and the mock
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
make gen                                          # compose.scopes.yaml + k8s/generated/
make up                                           # everything, Ollama included
make models                                       # pulls LLM_MODEL and EMBED_MODEL into the Ollama container
```

Ollama already on the host (Apple silicon, a GPU workstation): set `LLM_BASE_URL`, `EMBED_BASE_URL` and
`HINDSIGHT_EMBED_BASE_URL` to `http://host.docker.internal:11434` (`.../v1` for the last one) in `stack.env`, pull
the models with `ollama pull` on the host, and use `make up EXTRA="-f compose.host-ollama.yaml"`. NVIDIA inside
Docker: `EXTRA="-f compose.gpu.yaml"`.

Then, in this order:

1. **Sourcebot API key**: open http://localhost:3000, create the first (owner) account, Settings → API keys, and
   put the key in `stack.env` as `SOURCEBOT_API_KEY`; `make up` again so the gateway picks it up.
2. **Code graph**: `make index` runs one indexer job per scope (clone + `cgc index` into that scope's FalkorDB).
3. **Documents**: `make sync` asks the ingest to run every adapter for every scope. LightRAG processes the
   batch in the background with the model; `docker compose logs -f lightrag-<scope>` shows progress.
4. **Check isolation**: `make smoke` (identity headers forged directly against the gateway) and, once documents are
   processed, `make smoke ARGS=--live`.
5. **Put the SSO proxy in front** of `127.0.0.1:8090` (section 3, proxy). Every published port is bound to
   loopback; nothing else should be reachable from the network.
6. **Connect agents**: one URL per developer, see [CONNECT.md](CONNECT.md). Agents learn the routing and the
   recall-first / retain-last rule from the server itself (MCP instructions + prompts); CONNECT.md explains the
   limits and how to enforce `retain` with a client hook.

No Confluence or Backstage to test with yet? `make test-env` adds mock services that serve `test/fixtures` and
mounts `test/docs`; the shipped `scopes.yaml` already targets them, so steps 2–4 work unchanged.

Keeping it fresh: the ingest runs on `INGEST_SCHEDULE_CRON` and accepts `POST /webhook/<adapter>` with a filter
(`{"space": "ENG"}`, `{"repo": "https://..."}`); the indexer jobs run nightly on Kubernetes or from CI on push
(`.github/workflows/index-on-push.yml`); Sourcebot syncs on its own schedule.

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
kubectl -n context-stack port-forward svc/sourcebot 3000:3000    # create the API key, put it in stack.env,
                                                                  # make gen && kubectl apply -k k8s
kubectl -n context-stack create job --from=cronjob/indexer-public indexer-public-now   # per scope
kubectl -n context-stack port-forward svc/ingest 8080:8080 &  && make sync
kubectl -n context-stack port-forward svc/gateway 8090:8090 & && make smoke
```

Every Service is ClusterIP. Add an Ingress with OIDC auth in front of `svc/gateway` in your own overlay; never
expose the engine Services. Multi-node clusters: the per-scope `repos-<scope>` claim is shared by the codegraph pod
and its indexer job, so use a ReadWriteMany StorageClass or pin both to one node. More in
[../k8s/README.md](../k8s/README.md).

## 7. Adding a document source

Write a class deriving from `ingest/ingest/sources/base.py:Source` with one method, `documents(ctx, filter)`, that
yields `Document(key, version, text, title)`; the engine handles diffing, batching into LightRAG, deletions and
state. Declare it in `scopes.yaml` under `sources:` and reference it from a scope's `docs:`. The adapter must only
yield what the whole scope may read; anything with a finer ACL is skipped, never yielded. The four built-ins are the
reference implementations.

## 8. Upgrading and changing things

| Change | Do |
|---|---|
| `scopes.yaml` (groups, repos, docs) | `make gen`, then `make up` / `kubectl apply -k k8s`; a moved repo or space needs `make index` and `make sync` again |
| `stack.env` | `make up` (compose recreates changed containers) / `make gen && kubectl apply -k k8s` |
| gateway or ingest code | `make build && make up` / `docker compose build` + `kubectl rollout restart deploy/<name>` |
| engine versions | tags are pinned in `compose.yaml`, `scripts/gen-scopes.py` and `k8s/base/`; bump, re-verify the API notes in the README, redeploy |
| new scope | add it to `scopes.yaml`, `make gen`, deploy, `make index`, `make sync` — it costs one LightRAG + one FalkorDB/CodeGraphContext pair |
| removed scope | on Kubernetes also `kubectl -n context-stack delete all,pvc -l scope=<name>` |

## 9. Troubleshooting

- `gateway` refuses every call with "missing X-Forwarded-User": the request did not come through the proxy (or the
  smoke test URL is wrong). This is by design.
- `search_code` returns 401: no `SOURCEBOT_API_KEY`, or the key belongs to another Sourcebot instance (keys live in
  Sourcebot's database, so a fresh deployment needs a new one).
- `query_docs` answers "no context": LightRAG has not finished processing; check `/documents/pipeline_status` via
  `docker compose logs lightrag-<scope>` or `kubectl logs deploy/lightrag-<scope>`.
- Ingest logs "not configured": that adapter's credentials block in `stack.env` is empty.
- Hindsight restarts once or twice on a cold cluster: it waits for Postgres now; older manifests crash-looped until
  Postgres was up.
- A scope's indexer job fails with permission errors on `/home/cgc/.codegraphcontext`: the volume was created by an
  older image; delete the `cgc-<scope>` volume/claim and re-run.

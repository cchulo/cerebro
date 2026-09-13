# Setting up cerebro

One file to edit (`cerebro.yaml`), one file of secrets (`secrets.env`), one command per target. Read
[ACCESS-CONTROL.md](ACCESS-CONTROL.md) before pointing it at real data and [IDENTITY.md](IDENTITY.md) before
opening it to more than one person.

## 1. Prerequisites

- Python 3.12+ and [uv](https://github.com/astral-sh/uv) (or pip): `uv venv .venv && uv pip install -e '.[all]'`;
  the extras are `gateway`, `ingest`, `kubernetes`, `bridge`, `dev`.
- Docker with Compose v2 for `provisioning.target: compose`, or a cluster and `kubectl` for `kubernetes`.
- An inference backend: Ollama (in the stack, on the host, or elsewhere) with a chat model and the `bge-m3`
  embedding model, or an OpenAI-compatible endpoint your organisation approves. `gpt-oss:20b` needs about 16 GB of
  RAM on CPU.
- RAM: Postgres plus Hindsight about 3 GB, one LightRAG per scope about 1 GB each, one code unit per scope or repo
  (small), Keycloak about 1 GB in `builtin` mode, plus the model if Ollama runs here.

## 2. `cerebro.yaml`, section by section

Start from `cerebro.example.yaml`. Every key below is the schema in `cerebro/core/config.py`; `cerebro schema` prints
it as JSON schema for editor completion and `cerebro validate` checks a file and prints the resolved units. Any string
may contain `${NAME}` or `${NAME:-default}`, interpolated from the environment when the file is loaded.

| Key | Default | Meaning |
|---|---|---|
| `version` | `2` | schema version; only `2` is accepted |
| `identity.mode` | `none` | `none`, `static`, `builtin`, `external`; details in [IDENTITY.md](IDENTITY.md) |
| `identity.principal` | `{subject: local, groups: [everyone, admin]}` | mode `none`: who every request is |
| `identity.allow_remote` / `static_token_env` | `false` / `CEREBRO_TOKEN` | mode `none`: listen beyond loopback, then every request needs this bearer |
| `identity.tokens` | `{}` | mode `static`: token to principal map (`subject`, `groups`, `kind`, `token_scopes`) |
| `identity.issuer`, `audience`, `groups_claim`, `scope_claim` | none, none, `groups`, `scope` | `builtin` and `external`: the OAuth issuer and how claims map; `external` requires issuer and audience |
| `identity.token_validation`, `jwks_url`, `introspection` | `jwks`, from discovery, none | `introspection` needs `{url?, client_id, client_secret_env}` |
| `identity.server` | `{type: keycloak, realm: cerebro}` in `builtin` | `public_url` (browsers reach it there), `admin_user_env`, `admin_password_env`, `options` |
| `identity.users` | `[]` | `builtin`: `{name, groups, email?}` seeded once |
| `identity.legacy` | none | `{type: trusted_headers, user_header, groups_header}`: an SSO proxy's headers as a second provider |
| `policy.type`, `always_groups`, `team_banks_from_groups` | `groups`, `[everyone]`, `true` | IdP groups to scopes; `team-<group>` memory banks |
| `inference.llm` | `{provider: ollama, base_url: http://ollama:11434, model: gpt-oss:20b}` | `provider` is `ollama` or `openai`; `api_key_env` names a secret for `openai`; `options` pass through |
| `inference.embed` | `{... model: bge-m3, dim: 1024}` | same shape plus `dim`; Hindsight speaks OpenAI embeddings, so `/v1` is appended for Ollama |
| `inference.type` | `openai_compat` | the adapter cerebro itself would use for a model (`ollama` or `openai_compat`); engines get the endpoints through their unit env |
| `engines.docs` | `{type: lightrag, unit: scope}` | `options`: `max_async` 2, `max_parallel_insert` 1, `gleaning` 0, `think` false, `max_output_tokens` 4096, `schedule` (ingest cron, default `0 2 * * *`), `image`; `resources.storage` sizes the data volume; `idle_ttl` |
| `engines.code` | `{type: tokensave, unit: scope}` | `unit: scope` (repos side by side) or `repo`; `idle_ttl` (`2h`); `options.schedule` (index cron, default `0 3 * * *`); `resources` (`cpu`, `memory`, `storage` 10Gi) |
| `engines.memory` | `{type: hindsight}` | `options`: `reranker` `local` or `rrf`, `tenant`, `timeout`, `num_ctx`, `cp_access_key_secret` |
| `provisioning.target` | `compose` | `compose` or `kubernetes` |
| `provisioning.project`, `namespace`, `storage_class`, `image_registry` | `cerebro`, `cerebro`, none, none | compose project name and label; k8s namespace; PVC class; prefix for the images cerebro builds |
| `secrets.source`, `keys` | `env`, `[]` | every `${NAME}` any unit reads; compose takes them from `secrets.env`, kubernetes from the Secret it builds from it |
| `gateway.host`, `port`, `path` | `127.0.0.1`, `8090`, `/mcp` | the bind address (also the interface compose publishes on) and the MCP path |
| `gateway.public_url` | none | the URL clients use; it is the OAuth resource identifier (`aud`) |
| `gateway.state_type` | `json_file` | the ingest's `SyncState` adapter: `json_file` (one replica) or `postgres` |
| `gateway.plugins_dir`, `concurrency` | `plugins`, `8` | where `*.py` plugins live; parallel unit calls per request |
| `sources.<plugin>` | `{}` | non-secret plugin options; `sources.<plugin>.live` overrides the live part (`enabled`, `fallback`, `via`, `url`) |
| `scopes.<name>.groups` | `[]` | IdP groups that may read the scope (`everyone` = any authenticated caller) |
| `scopes.<name>.code.repos` | `[]` | `https://...git` strings (default branch) or `{url, branches: [main, "release/*"]}`; `code.unit` overrides `engines.code.unit` |
| `scopes.<name>.docs.<plugin>` | `{}` | what of that source belongs here (`confluence: {spaces: [ENG]}`, `git: {globs: [...]}`, `files: {paths: [...]}`, `jama: {projects: [42]}`, `backstage: {}`) |

Scope names are DNS labels; a repository or a Confluence space may appear in exactly one scope (the loader refuses).

## 3. `secrets.env`

`secrets.env.example` lists every name the planner may need with generation hints; copy it and fill what you use.
Names the plan references must also be in `secrets.keys` (the plan warns otherwise). On Kubernetes every key a pod
references must exist in the file (an empty value is fine) or the pod does not start.

| Name | Needed by |
|---|---|
| `POSTGRES_PASSWORD` | postgres, LightRAG, Hindsight, Keycloak, the postgres sync state |
| `LIGHTRAG_API_KEY`, `HINDSIGHT_API_KEY` | the engines and the gateway / ingest that call them |
| `INGEST_WEBHOOK_SECRET` | `POST /sync/*` and `/webhook/*` on the ingest service (empty = open) |
| `CEREBRO_TOKEN` | mode `none` with `allow_remote` |
| `CEREBRO_AUTH_ADMIN_USER`, `CEREBRO_AUTH_ADMIN_PASSWORD`, `CEREBRO_SEED_PASSWORD` | mode `builtin` (Keycloak bootstrap admin; optional first-login password) |
| `GITHUB_TOKEN` | code units and index jobs, the `git` docs plugin (private repositories) |
| `CONFLUENCE_URL`, `CONFLUENCE_USER`, `CONFLUENCE_TOKEN`, `CONFLUENCE_PERSONAL_TOKEN` | confluence plugin and the `mcp-confluence` upstream |
| `BACKSTAGE_URL`, `BACKSTAGE_TOKEN`; `JAMA_URL`, `JAMA_USER`, `JAMA_TOKEN` | those plugins |
| whatever `api_key_env`, `client_secret_env`, `cp_access_key_secret` name | `openai` inference, introspection, Hindsight UI |

## 4. Inference backends

| Setup | `cerebro.yaml` | Effect |
|---|---|---|
| Ollama in the stack (default) | `base_url: http://ollama:11434` on both endpoints | the plan adds an `ollama` unit (`ollama/ollama:0.33.3`, 60Gi volume); pull models once: `docker compose -p cerebro -f deploy/generated/compose.yaml exec ollama ollama pull gpt-oss:20b` (and `bge-m3`) |
| Ollama on the host | `LLM_BASE_URL=http://host.docker.internal:11434 EMBED_BASE_URL=http://host.docker.internal:11434 cerebro provision up` (the example file interpolates both) | no `ollama` unit is planned (verified with `provision plan`); Docker Desktop and OrbStack resolve that name, plain Docker Engine on Linux needs the host reachable another way |
| Org-approved OpenAI-compatible endpoint | `provider: openai, base_url: https://llm.internal/v1, model: ..., api_key_env: LLM_API_KEY` | LightRAG and Hindsight get the URL and `${LLM_API_KEY}` in their env; add the name to `secrets.keys` |

Only LightRAG and Hindsight talk to it. Thinking models should be run with reasoning off (`engines.docs.options.think: false`
is the default) or extraction becomes slow and expensive.

## 5. Docker Compose lifecycle

```sh
cerebro provision plan                        # units and jobs, images, ports, secrets each needs; nothing written
cerebro provision render                      # deploy/generated/compose.yaml (also what `up` does first)
cerebro provision up [unit ...]               # render, `docker compose up -d`, wait for health; listed units only if given
cerebro provision status [unit ...]           # ready | starting | stopped | absent, from `docker compose ps`
cerebro provision job index-code-public --wait   # run a job now (profile `jobs`)
cerebro provision down [--volumes]            # stop and remove; --volumes deletes every data volume
```

All take `-c cerebro.yaml`, `--target`, `--env-file secrets.env` and `-o deploy/generated`. What the rendered file
contains, read from `deploy/generated/compose.yaml`:

- Images with a `build:` (`cerebro/gateway`, `cerebro/ingest`, `cerebro/code-unit:<cerebro version>`) are built by
  compose from `images/` on the first `up`; the rest are pulled. `provisioning.image_registry` prefixes the built ones.
- Only the gateway publishes a port, on `gateway.host:gateway.port`. Engines, Postgres, Ollama and MCP upstreams are
  reachable on the compose network only, by unit name.
- `cerebro.yaml` and `plugins/` are bind-mounted read-only into `gateway` and `ingest`; secrets are `${NAME}` references
  resolved from `--env-file` at run time.
- Jobs are services under the `jobs` profile. Scheduled ones get a `scheduler` service: `docker:27-cli` running busybox
  crond with one line per job that executes `docker compose ... --profile jobs run --rm <job>` through the host's
  Docker socket, mounted read-write, with the repository mounted read-only. That container can do anything the Docker
  daemon can; it is a dev convenience, not something to run on a shared host. On Kubernetes the same jobs are CronJobs.
- Health checks: `pg_isready` and `ollama list` for the base units; an HTTP probe on `/health` for the rest (compose
  tries curl, wget, then python3 inside the image).

In identity mode `none` the gateway binds the container's loopback and the published port answers nothing; use
`allow_remote` with `CEREBRO_TOKEN` and `gateway.host: 0.0.0.0` as in the README, or a real identity mode.

## 6. Kubernetes

```sh
cerebro provision render --target kubernetes       # deploy/generated/k8s/: namespace, one file per unit and job, kustomization
cp secrets.env deploy/generated/k8s/               # `up` does this; the Secret cerebro-secrets is built from it at apply time
kubectl apply -k deploy/generated/k8s
cerebro provision up --target kubernetes           # render, copy, apply, then ensure() every unit through the API and wait
cerebro provision operator                         # the idle-TTL operator (kopf), against provisioning.namespace
```

- Plain objects, no CRD: a `Service` and a `Deployment` (or `StatefulSet` for Postgres) per unit, a PVC per volume
  (`<unit>-<volume>`, `ReadWriteOnce` unless `provisioning.options.shared_access_mode` says otherwise; a job shares
  its unit's claim, so both land on one node), a `CronJob` per job (suspended when it has no schedule, so
  `provision job` has a template to instantiate), `ConfigMap`s for `cerebro.yaml`, `plugins/` and init scripts.
- Secrets are `valueFrom.secretKeyRef` entries into `cerebro-secrets`; `${NAME}` inside other env values becomes
  `$(NAME)`. Nothing rendered holds a value.
- Images: build `images/gateway`, `images/ingest`, `images/code-unit` yourself and make them pullable (set
  `provisioning.image_registry`); `imagePullPolicy: IfNotPresent` is rendered for them.
- Ingress is yours: every Service is `ClusterIP`. Route your TLS Ingress to `gateway` (and to `auth` in `builtin` mode);
  never expose engine Services.
- Idle TTL: docs and code Deployments carry `cerebro.io/idle-ttl` (from `engines.<kind>.idle_ttl`); the operator
  compares `cerebro.io/last-used` (stamped by `ensure()` and `touch()`, or the creation time) with it every 60 s and
  patches `replicas: 0`. Waking up is `cerebro provision up <unit>` (or `kubectl scale`): the gateway does not call
  `ensure()` on a request today, so an idled unit stays at zero until you scale it.
- A scope removed from `cerebro.yaml` leaves its objects behind (render only writes current units):
  `kubectl -n cerebro delete deploy,svc,cronjob,pvc -l cerebro.io/scope=<name>`. `provision down` deletes everything
  with the `cerebro.io/project` label; `--volumes` also the claims and the namespace.

The kubernetes adapter is verified by unit tests on the rendered objects and the operator's decision function; it
has not been applied to a live cluster in v2.

## 7. Indexing code

`cerebro index` is what the `index-<unit>` job runs inside the code-unit image: clone or fetch each repository of the
unit into `/workspace/<dir>` (blobless clone), `tokensave init` or `sync` on the default branch, `checkout -B` plus
`tokensave branch add` and `sync` for every configured branch (globs resolved against origin), `branch remove` for
branches no longer configured, then back to the default branch for ripgrep. Skips are per repo and branch from
`.cerebro-index.json`, so running it often is cheap.

```sh
cerebro provision job index-code-public --wait                 # compose or kubernetes, through the provisioner
cerebro index run --unit code-public --workspace /workspace    # inside the unit image (CEREBRO_REPOS set by the unit)
cerebro index run --unit code-public -c cerebro.yaml --workspace ./ws --tokensave /path/to/tokensave   # on a host that has the binary
cerebro index all -c cerebro.yaml --workspace ./ws             # every unit into ./ws/<unit>/
```

`--force` re-indexes (`tokensave sync --force`). The schedule is `engines.code.options.schedule` (default `0 3 * * *`).
`GITHUB_TOKEN` is passed to git through `GIT_CONFIG_*` (never on a command line).

## 8. Syncing documents

The `ingest` unit runs the FastAPI service: cron from `engines.docs.options.schedule` (default `0 2 * * *`), and
`POST /sync/all`, `POST /sync/<plugin>`, `POST /webhook/<plugin>` (body = the plugin's filter, e.g.
`{"space": "ENG"}` or `{"repo": "https://github.com/org/x.git"}`), all guarded by `X-Ingest-Secret`. One sync runs at
a time. `GET /health` reports every scope's index health; `GET /sources` which plugins are configured.

```sh
cerebro ingest -c cerebro.yaml sync                      # every plugin, every scope; report as JSON
cerebro ingest -c cerebro.yaml sync confluence --scope payments --filter '{"space": "PAY"}'
cerebro ingest -c cerebro.yaml check payments confluence --limit 5    # what a plugin yields, no index touched
```

Note the position of `-c`: for `cerebro ingest` it precedes the subcommand. From the host, unit names such as
`docs-payments` do not resolve, so run these inside the container:
`docker compose -p cerebro -f deploy/generated/compose.yaml exec ingest cerebro ingest sync` (the image sets
`CEREBRO_CONFIG`). Only changed documents are sent; LightRAG extracts in the background, which is the slow part
(`GET /documents/pipeline_status` on the docs unit, or `ingest`'s `/health`, shows progress). Sync state lives in
`/state/versions.json` on the ingest volume (`gateway.state_type: json_file`) or in the `cerebro` database
(`postgres`) when you run more than one ingest replica.

## 9. Troubleshooting

| Symptom | Cause |
|---|---|
| published gateway port answers nothing, container healthy | identity mode `none` binds loopback inside the container (section 5) |
| `401 invalid_token` on every call | no or wrong bearer; in `builtin`/`external` the `WWW-Authenticate` header names the metadata URL your client should follow |
| `RuntimeError: identity adapter bearer_jwt needs identity.issuer` | `builtin` mode without an explicit `identity.issuer` (set it to `<public_url>/realms/<realm>`) |
| `no docs adapter ...` note in `/health` | the `type:` names an adapter module that does not exist or lacks its extra |
| `query_docs` answers with `answered: false` and `fallback` | LightRAG has not finished extracting, or the index has nothing; the live sources were searched |
| `not indexed yet` from a code tool | the `index-<unit>` job has not run for that repository |
| plan warns `references secret X which is not declared in secrets.keys` | add `X` to `secrets.keys` (and to `secrets.env`) |
| kustomize `secrets.env not found` | copy it next to the kustomization or run `provision up`, which copies it |

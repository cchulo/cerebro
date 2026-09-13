# tests/e2e: the example stack, for real, against fake data

The v2 stack on Docker Compose with the mock Confluence and Backstage of `tests/mock` (serving
`tests/fixtures/sources`), the fixture docs of `tests/fixtures/docs`, the public pallets repositories, the host's
Ollama, and static bearer tokens for the demo personas. `cerebro smoke` then plays every persona through the
gateway and checks what each may and may not see. Verified 2026-09-13 (`qwen3.6:35b-mlx` + `bge-m3` on an M-series
Mac with OrbStack): code indexing 2-6 s per unit, the 23 fixture documents extracted in 82 s, the full smoke test
(168 checks, `--live`) in about 2 minutes.

| file | what |
|---|---|
| `cerebro.e2e.yaml` | the example config with `identity.mode: static`, host Ollama, the mocks as sources, gateway on 8091 |
| `secrets.env.example` | every `${NAME}` the config and the units read; copy to `secrets.env` (gitignored) with fresh values |
| `compose.mocks.yaml` | the mocks joined to the stack, `tests/fixtures/docs` mounted into the ingest, ingest port published |
| `probes.yaml` | marker probes for `cerebro smoke --probes`: which persona must and must not see `ZEPHYR-7731`, `RESTRICTED-QX-9911`, `KESTREL-5520`, and the live Confluence checks |

Personas (`identity.tokens`): alice `payments-team` (public + payments), bob `sre` (public + infra), carol
`platform-leads` (everything), dave no group (public only), ci-bot a service token with `cerebro:code.read` only.

## Prerequisites

Docker with Compose v2 (OrbStack or Docker Desktop), Ollama on the host with a chat model and `bge-m3` pulled
(`ollama list`; the config defaults to `qwen3.6:35b-mlx`, override with `LLM_MODEL=... ` in `secrets.env`), and
this checkout installed: `uv venv .venv && uv pip install -e '.[all]'`. Port 8091 (gateway) and 8081 (ingest) free
on 127.0.0.1.

## Run

```sh
# 1. secrets (random values; the mocks accept any credentials)
python3 -c 'import re,secrets;print(re.sub("CHANGE_ME",lambda m:secrets.token_hex(24),open("tests/e2e/secrets.env.example").read()),end="")' > tests/e2e/secrets.env

# 2. render, check, build, start (provisioning.options.compose_files adds compose.mocks.yaml to every compose command)
cerebro provision render -c tests/e2e/cerebro.e2e.yaml --env-file tests/e2e/secrets.env --target compose
docker compose -p cerebro -f deploy/generated/compose.yaml -f tests/e2e/compose.mocks.yaml --env-file tests/e2e/secrets.env --profile jobs config > /dev/null
docker compose -p cerebro -f deploy/generated/compose.yaml -f tests/e2e/compose.mocks.yaml --env-file tests/e2e/secrets.env --profile jobs build
cerebro provision up -c tests/e2e/cerebro.e2e.yaml --env-file tests/e2e/secrets.env        # waits for every unit's health

# 3. index the code (one job per unit; the unit names come from `cerebro provision plan -c ...`)
cerebro provision job index-code-public --wait -c tests/e2e/cerebro.e2e.yaml --env-file tests/e2e/secrets.env
cerebro provision job index-code-infra --wait -c tests/e2e/cerebro.e2e.yaml --env-file tests/e2e/secrets.env
cerebro provision job index-code-payments-jinja-github-com-pallet-737f99 --wait -c tests/e2e/cerebro.e2e.yaml --env-file tests/e2e/secrets.env

# 4. sync the documents, then watch each scope until N/N processed [idle] (about 90 s with the MLX model)
curl -X POST localhost:8081/sync/all -H "X-Ingest-Secret: $(grep ^INGEST_WEBHOOK_SECRET= tests/e2e/secrets.env | cut -d= -f2)"
for s in public payments infra; do docker compose -p cerebro exec -T docs-$s python -c '
import json,os,urllib.request; h={"X-API-Key":os.environ["LIGHTRAG_API_KEY"]}
g=lambda p: json.load(urllib.request.urlopen(urllib.request.Request("http://localhost:9621"+p,headers=h)))
c=g("/documents/status_counts")["status_counts"]; print(c.get("processed",0),"/",c.get("all",0),"busy" if g("/documents/pipeline_status").get("busy") else "idle")'; done

# 5. the smoke test: every persona, marker probes, live Confluence through mcp-atlassian
cerebro smoke -c tests/e2e/cerebro.e2e.yaml --env-file tests/e2e/secrets.env --probes tests/e2e/probes.yaml --live
cerebro smoke -c tests/e2e/cerebro.e2e.yaml --env-file tests/e2e/secrets.env --as alice --only identity,code   # one persona, some parts
cerebro smoke -c tests/e2e/cerebro.e2e.yaml --url http://127.0.0.1:8091/mcp --as alice --token <t>            # any deployment

# 6. tear down (keeps the images)
cerebro provision down --volumes -c tests/e2e/cerebro.e2e.yaml --env-file tests/e2e/secrets.env
```

`cerebro provision status -c tests/e2e/cerebro.e2e.yaml --env-file tests/e2e/secrets.env` shows every unit;
`docker compose -p cerebro logs -f gateway` shows every tool call. The gateway's `/health` is at
http://127.0.0.1:8091/health. Everything the provisioner creates carries the label `cerebro.io/project=cerebro`.

## What the smoke test checks

Expectations are computed from the config with the same policy adapter the gateway runs (`cerebro.smoke`):

- identity: `whoami` / `list_scopes` (scopes, repos, banks, token scopes) per persona; `tools/list` hides tools the
  token lacks; ci-bot's `query_docs` is refused
- docs: `query_docs` on a scope outside the grants is refused; the index fans out to exactly the caller's scopes
- code: `search_code` hits only repositories of the caller's scopes (dave: click and flask only); `list_code_units`
  lists only the caller's units; `code_tool` on a foreign unit is refused; `branch: stable` works on flask and an
  unknown branch is refused, for `search_code` and `code_tool`
- memory: retain + recall on the personal bank and every team bank; another persona's bank is refused
- probes: `ZEPHYR-7731` (PAY) for alice and carol, never for bob or dave; `RESTRICTED-QX-9911` (restricted page) for
  nobody; `KESTREL-5520` (OPS) for bob and carol, never for alice or dave
- live: `live_search` confined to the caller's spaces, page 3002 never listed; `live_fetch` of a PAY page refused
  for bob and dave and of the restricted page refused for everyone; `query_docs` fallback reaches Confluence

Same test in process against the fake gateway: `pytest tests/gateway/test_smoke.py`.

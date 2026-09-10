# The demo: an agent working across docs, code and memory, with the stack visible

One script brings up the whole stack against **fake organisation data**, hooks Claude Code or Cursor up as one of
four demo users, shows what happens inside the stack while the agent works, and tears everything down again.

```sh
scripts/demo.sh up                      # 1. everything (first run: ~10 min incl. model pull and repo cloning)
scripts/demo.sh watch                   # 2. wait until every scope reads N/N processed [idle]  (Ctrl-C to leave)
scripts/demo.sh connect --as alice      # 3. the Claude Code / Cursor commands for this persona (--write drops them in the repo)
scripts/demo.sh activity                # 4. second terminal: gateway tool calls in white, engines in green
scripts/demo.sh down --volumes          # 5. all gone (without --volumes the data stays for a faster next 'up')
```

Add `--k8s` to every command to run it on Kubernetes (OrbStack, k3s) instead of Docker Compose. `make demo ARGS=...`
is the same thing through make.

## What is fake and what is real

| Source | In the demo | Where it comes from |
|---|---|---|
| Confluence | mock server, four spaces (ENG, DOCS, PAY, OPS), one page restricted | `test/fixtures/confluence.yaml`, served by `test/mock` |
| Backstage | mock catalog: checkout, ledger, ledger-api, commerce, pg-main, two teams | `test/fixtures/backstage.yaml` |
| Jama Connect | mock: requirements and a test case in projects 42 (payments) and 57 (infra) | `test/fixtures/jama.yaml` |
| Local files | an exported architecture overview and an on-call handbook | `test/docs/` |
| Code | **real** public repositories: pallets/click, flask (public scope), jinja (payments), werkzeug (infra) | `config/scopes.yaml`, cloned by Sourcebot and the indexer |
| The engines | real: LightRAG, Sourcebot, CodeGraphContext + FalkorDB, Hindsight, mcp-atlassian | pinned images |
| Inference | your Ollama (host or in-stack) or the org endpoint in `config/stack.env` | nothing leaves the machine |

The fixtures tell one small story: a commerce platform (checkout in Python, ledger in Go) that settles card payments
through an acquirer's SFTP. The pages, catalog entries and requirements reference each other the way real ones do,
so a good answer needs several sources. Every document carries a marker code (`ZEPHYR-7731`, `JAMA-RETRY-5150`, ...)
so you can tell at a glance which source an answer came from.

## Prerequisites

- Docker (Docker Desktop or OrbStack), or a Kubernetes cluster with `kubectl` for `--k8s`.
- A model endpoint. Easiest: [Ollama](https://ollama.com) running on the host with a generative model and `bge-m3`
  pulled; `up` detects it, picks the strongest pulled model it knows, and pulls what is missing. Without host Ollama
  the stack starts its own Ollama container (CPU-only on a Mac, slow). Any OpenAI-compatible endpoint your
  organisation controls works too: set `LLM_PROVIDER=openai` and the URLs in `config/stack.env` before `up`.
- `python3`. PyYAML and the `mcp` client are needed by the scripts; if the system python lacks them, `up` creates
  `.demo/venv` from `scripts/requirements.txt` and uses that.
- Claude Code or Cursor on the same machine, to play the agent.

### The Sourcebot API key (one manual step, on purpose)

Code search needs an API key that only Sourcebot's own UI can issue, so `up` stops with instructions the first time:

1. Open http://127.0.0.1:3000 (compose; on Kubernetes `kubectl -n context-stack port-forward svc/sourcebot 3000:3000`).
2. Register the first user. It is a local account inside this Sourcebot; any email and password will do, and that
   first user becomes the owner.
3. Settings > API Keys > create one, and paste it into `config/stack.env` as `SOURCEBOT_API_KEY=...`.
4. `scripts/demo.sh up` again: the gateway is recreated with the key; `ready` confirms the key works.

The key lives in Sourcebot's database, so `down --volumes` deletes it and the next `up` asks again. This is also
where you can watch the repositories being cloned and indexed.

### The GitHub token

Sourcebot and the indexer clone the repositories listed in `config/scopes.yaml`. The four demo repositories are
public, so no GitHub token is required, but Sourcebot lists them through the GitHub API, which allows only 60
unauthenticated requests per hour per IP. If Sourcebot logs rate-limit errors, or the moment you add a private
repository, put a token in `config/stack.env`:

```
GITHUB_TOKEN=github_pat_...        # fine-grained token, read-only "Contents" and "Metadata" on the repositories
```

`config/sourcebot/config.json` references it as `{ "env": "GITHUB_TOKEN" }`; the private-org form is in
`config/sourcebot/config.private-org.example.json`. After changing it: `make gen && make up` (a plain restart keeps
old environment).

## Step by step

### 1. `scripts/demo.sh up`

Creates `config/stack.env` if it does not exist (random secrets, host Ollama detected, model chosen), generates the
per-scope services, builds the images, starts everything with the mocks, clones and indexes the code per scope and
triggers the document sync. Each phase prints with a timestamp. The script ends with a **NOT READY YET** banner:
the ingest hands documents to LightRAG at once, but extraction into the graph runs in the background.

### 2. `scripts/demo.sh watch`

The status screen every 5 seconds. Ready means every scope shows `N/N processed [idle]` and the code-graph line
shows every repository indexed. With `qwen3.6:35b-mlx` on an M-series laptop the 23 fixture documents take about
80 seconds; the first `up` also spends a few minutes cloning and indexing the repositories.
`scripts/demo.sh ready` prints the same verdict once and exits 0 when ready.

### 3. `scripts/demo.sh connect --as alice`

Prints the exact commands for Claude Code and the JSON for Cursor, with the identity headers for that persona.
`--write` puts them into the repository as `.mcp.json` (Claude Code, project scope) and `.cursor/mcp.json`; open the
repository in either tool and the `context` server is there. Personas:

| Persona | Groups | Sees |
|---|---|---|
| alice | payments-team | public + payments: PAY space, Jama project 42, jinja, banks `user-alice` + `team-payments-team` |
| bob | sre | public + infra: OPS space, Jama project 57, werkzeug, on-call handbook, `team-sre` |
| carol | platform-leads | everything, `team-platform-leads` |
| dave | none | public only: ENG + DOCS spaces, Backstage, click + flask |

The identity is **forged by the client** (the headers an SSO proxy would set). That is what makes the demo
self-contained, and exactly what the production setup must prevent: there the gateway is reachable only through the
proxy ([ACCESS-CONTROL.md](ACCESS-CONTROL.md)).

### 4. `scripts/demo.sh activity` in a second terminal

A live trail: every tool call through the gateway (who, which tool, the arguments, how long) in white, and what
the engines do to answer (LightRAG keyword extraction and query, Hindsight recall/retain, Sourcebot searches,
mcp-atlassian calls, ingest runs) in green. Errors in red. `--all` shows every log line.

## The storyline

Play it as **alice** (payments engineer). Each step needs a different source or a different engine; the marker
codes in brackets tell you the answer was grounded.

1. **Start the task.** In Claude Code: `/mcp__context__start_task investigate this afternoon's settlement upload failures`.
   The agent calls `recall` first (empty on the first run) and `list_scopes`.

2. **Docs across four sources.** Ask:
   > The acquirer SFTP upload for today's settlement failed twice. Who owns the ledger service and what does it depend on, what is the retry requirement and the test that covers it, when is the settlement cutoff, and how do we deploy and roll back checkout if a fix is needed?

   A grounded answer cites the Backstage catalog (ledger owned by payments-team, checkout depends on ledger and pg-main
   owned by sre), Jama PAY-REQ-1 (retry every 15 minutes for 6 hours, page after the 24th failure, `JAMA-RETRY-5150`)
   and PAY-TC-7 (`JAMA-TC-2424`), the PAY space (cutoff 14:30 UTC, `ZEPHYR-7731`) and the ENG deploy runbook
   (`shipit deploy checkout`, `ENG-DEPLOY-3302`). Watch `activity`: one `query_docs` fans out to the public and payments
   graphs in parallel.

3. **Code search and the code graph.** Ask:
   > In the jinja repository, where is `Environment.get_template` defined and which functions call it? Is there any dead code around the template cache?

   `search_code` (Sourcebot, restricted to jinja for alice) finds the definition; `code_graph` with
   `analyze_code_relationships` / `find_dead_code` answers the structural part from the payments scope's graph.

4. **What alice cannot see.** Ask:
   > What is the failover procedure for the pg-main cluster and its RTO?

   Those live in the OPS space and Jama project 57 (infra). Alice's `query_docs` has no answer for them; the
   fallback searches only her Confluence spaces, so nothing leaks. Reconnect as **bob** and ask again: `patronictl
   switchover` (`KESTREL-5520`) and 120 seconds (`JAMA-RTO-1200`). Ask anyone about fraud model thresholds: the page
   is restricted and was never indexed (`RESTRICTED-QX-9911` never appears).

5. **The index lags, the fallback does not.** Run `scripts/demo.sh add-page`: it writes a postmortem page into the
   mock Confluence *after* the sync. Ask alice:
   > Is there a postmortem for the settlement SFTP outage? What was the root cause and the action item?

   `query_docs` misses, the gateway searches Confluence live through mcp-atlassian for alice's spaces, returns the
   hit, and the agent reads it with `live_fetch` (`LATE-PAGE-0042`). Then `make sync` and ask again: now it is indexed.

6. **Wrap up, and memory pays off.** `/mcp__context__wrap_up`: the agent calls `retain` with what it learned
   (Hindsight extracts facts in the background). Start a new session, `/mcp__context__start_task follow up on the
   settlement outage`: `recall` brings the previous outcome back. Retain to `team-payments-team` and **carol** sees it
   too; **bob** does not.

Prompts for Cursor are the same sentences; Cursor shows the server's prompts in its prompt picker and lists the
tools under Settings, MCP.

## Self-check and teardown

`scripts/demo.sh smoke` runs the access-control test with the activity trail, the quickest way to prove the stack
is healthy before an audience. `scripts/demo.sh down` stops the containers, removes the late page and the written
client configs; `down --volumes` also deletes every data volume (Postgres, LightRAG graphs, repositories, memory).
Both select by label, so nothing else on the machine is touched. `config/stack.env` is kept.

## If something is off

- `query_docs` returns "no context" for everything: not ready yet, or the model endpoint was down during extraction.
  `scripts/demo.sh ready`; failed documents show as `N failed` and are retried with `make sync`.
- After `make sync` the answer about the late page is still the old one: an older generated `compose.scopes.yaml`
  without `ENABLE_LLM_CACHE=false` (run `make gen && make up`); LightRAG's query cache otherwise repeats stale answers.
- The agent never calls `recall`/`retain`: the server instructions ask for it, the prompts force it; a Claude Code
  Stop hook can require it ([CONNECT.md](CONNECT.md)).
- Slow answers: every LightRAG query is two model calls; see [SETUP.md, Performance](SETUP.md#11-performance-what-the-model-endpoint-has-to-do).
- Kubernetes: `scripts/demo.sh connect --k8s` reminds you to keep `kubectl -n context-stack port-forward svc/gateway 8090:8090` running.

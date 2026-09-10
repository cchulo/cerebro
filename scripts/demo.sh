#!/usr/bin/env bash
# One entry point for the demo: fake organisation data (mock Confluence, Backstage and Jama serving test/fixtures,
# local files under test/docs, public GitHub repositories), the whole stack, a connection for Claude Code or Cursor,
# live activity while you talk to it, and teardown. Walkthrough with the storyline: docs/DEMO.md
#
#   scripts/demo.sh up [--k8s]                 create config/stack.env if missing, start everything, index code, sync docs
#   scripts/demo.sh ready [--k8s]              has every scope finished ingesting? (exit 0 = yes)
#   scripts/demo.sh watch [--k8s]              progress screen, refreshing until Ctrl-C
#   scripts/demo.sh activity [--k8s]           live trail: gateway tool calls (white), what the engines do (green)
#   scripts/demo.sh personas                   the demo users and what each may see
#   scripts/demo.sh connect --as <persona> [--k8s] [--write]
#                                              how to hook Claude Code / Cursor up as that person (--write: into this repo)
#   scripts/demo.sh add-page [--k8s]           simulate a Confluence page written after the last sync (live-fallback demo)
#   scripts/demo.sh smoke [--k8s]              the access-control test with live activity, as a self-check
#   scripts/demo.sh down [--volumes] [--k8s]   stop everything; --volumes deletes the data too
#
# Identity: the script forges the headers an SSO proxy would set (X-Forwarded-User / X-Forwarded-Groups). Acceptable
# on a laptop with the gateway bound to 127.0.0.1, never in production (docs/ACCESS-CONTROL.md).
set -euo pipefail
cd "$(dirname "$0")/.."
CMD=${1:-help}; [ $# -gt 0 ] && shift
K8S=0 VOLUMES=0 WRITE=0 AS=""
while [ $# -gt 0 ]; do
  case $1 in --k8s) K8S=1;; --volumes|-v) VOLUMES=1;; --write) WRITE=1;; --as) AS=$2; shift;;
    -h|--help) CMD=help;; *) echo "unknown option $1" >&2; exit 2;; esac
  shift
done
K=$([ $K8S = 1 ] && echo --k8s || true)
B=$'\033[1m'; Y=$'\033[1;33m'; G=$'\033[32m'; D=$'\033[2m'; N=$'\033[0m'
[ -t 1 ] || B="" Y="" G="" D="" N=""
say()  { printf '%s\n' "$*"; }
head_() { printf '\n%s%s%s\n' "$B" "$*" "$N"; }
warn() { printf '%s%s%s\n' "$Y" "$*" "$N"; }

# ---------------------------------------------------------------------------------------------- personas
# name -> IdP groups. Scopes come from config/scopes.yaml: public = everyone, payments = payments-team + platform-leads,
# infra = sre + platform-leads. Every persona also has a personal memory bank; groups add team banks.
persona_groups() {
  case $1 in
    alice) echo "payments-team";;                 # payments engineer: public + payments
    bob)   echo "sre";;                           # SRE: public + infra
    carol) echo "platform-leads";;                # platform lead: everything
    dave)  echo "";;                              # contractor, no group: public only
    *) return 1;;
  esac
}
personas() {
  head_ "Demo personas (identity is forged by the MCP client; the gateway decides what each may see)"
  say   "  alice   payments-team    scopes public + payments : PAY space, Jama project 42, jinja repo, banks user-alice + team-payments-team"
  say   "  bob     sre              scopes public + infra    : OPS space, Jama project 57, werkzeug repo, on-call handbook, team-sre"
  say   "  carol   platform-leads   every scope              : all of the above, team-platform-leads"
  say   "  dave    (no group)       scope public only        : ENG + DOCS spaces, Backstage catalog, click + flask repos"
  say   ""
  say   "  Nobody sees the restricted PAY page (fraud thresholds): it carries a page-level restriction and is never indexed."
}

# ---------------------------------------------------------------------------------------------- helpers
scopes() { $PY -c "import yaml;print(' '.join(yaml.safe_load(open('config/scopes.yaml'))['scopes']))"; }
ensure_python() {
  # up.sh, gen-scopes.py and the smoke test need PyYAML and the mcp client; use the system python if it has them,
  # otherwise a private venv under .demo/ (created once).
  if python3 -c "import yaml, mcp" 2>/dev/null; then PY=python3; return; fi
  if [ ! -x .demo/venv/bin/python ]; then
    say "  ${D}python3 lacks PyYAML/mcp: creating .demo/venv from scripts/requirements.txt${N}"
    python3 -m venv .demo/venv && .demo/venv/bin/pip -q install -r scripts/requirements.txt
  fi
  PY=$PWD/.demo/venv/bin/python
}
compose_files() {
  FILES=(-f docker/compose.yaml -f docker/compose.scopes.yaml -f docker/compose.test.yaml)
  grep -q "host.docker.internal" config/stack.env 2>/dev/null && FILES+=(-f docker/compose.host-ollama.yaml)
  export COMPOSE_ENV_FILES="$PWD/config/stack.env"
}
dc() { docker compose "${FILES[@]}" "$@"; }
rand() { openssl rand -hex "${1:-24}"; }
host_ollama_up() { curl -fsS -m 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; }
host_has_model() { curl -fsS -m 2 http://127.0.0.1:11434/api/tags 2>/dev/null | grep -q "\"name\":\"$1\""; }
env_get() { grep "^$1=" config/stack.env | head -1 | cut -d= -f2-; }
env_set() { sed -i '' "s|^$1=.*|$1=$2|" config/stack.env; }

bootstrap_env() {
  [ -f config/stack.env ] && return 0
  head_ "config/stack.env does not exist: creating it from config/stack.env.example with fresh secrets"
  cp config/stack.env.example config/stack.env
  env_set POSTGRES_PASSWORD "$(rand 16)";      env_set HINDSIGHT_API_KEY "$(rand)";  env_set HINDSIGHT_CP_ACCESS_KEY "$(rand 12)"
  env_set LIGHTRAG_API_KEY "$(rand)";          env_set INGEST_WEBHOOK_SECRET "$(rand)"
  env_set SOURCEBOT_AUTH_SECRET "$(openssl rand -base64 33)"; env_set SOURCEBOT_ENCRYPTION_KEY "$(openssl rand -base64 24)"
  if host_ollama_up; then
    say "  Ollama is running on this host: the stack will use it (host.docker.internal)"
    for v in LLM_BASE_URL EMBED_BASE_URL; do env_set $v "http://host.docker.internal:11434"; done
    env_set HINDSIGHT_EMBED_BASE_URL "http://host.docker.internal:11434/v1"
    chosen=""
    for m in qwen3.6:35b-mlx qwen3.6:35b gpt-oss:20b qwen3.5:9b gemma4:31b-mlx llama3.3:latest; do host_has_model "$m" && { chosen=$m; break; }; done
    if [ -n "$chosen" ]; then env_set LLM_MODEL "$chosen"; say "  generative model: $chosen (already pulled)"
    else say "  no known generative model pulled yet: keeping $(env_get LLM_MODEL); it is pulled at 'up' (large download)"; fi
  else
    warn "  no Ollama on this host: the stack starts its own Ollama container (CPU-only on a Mac: slow). Point"
    warn "  LLM_BASE_URL / EMBED_BASE_URL in config/stack.env at an endpoint your organisation controls to avoid that."
  fi
  say "  written: config/stack.env (gitignored). Secrets are random; edit GITHUB_TOKEN if you want private repositories."
}

pull_models() {
  llm=$(env_get LLM_MODEL); emb=$(env_get EMBED_MODEL)
  if grep -q "host.docker.internal" config/stack.env; then
    for m in "$llm" "$emb"; do
      host_has_model "$m" && continue
      head_ "pulling $m into the host Ollama (one-time download)"; ollama pull "$m"
    done
  elif [ $K8S = 1 ]; then
    for m in "$llm" "$emb"; do head_ "pulling $m into the in-cluster Ollama"; kubectl -n context-stack exec deploy/ollama -- ollama pull "$m"; done
  else
    for m in "$llm" "$emb"; do head_ "pulling $m into the Ollama container"; dc exec ollama ollama pull "$m"; done
  fi
}

probe() {   # per scope: "<processed>/<total> [busy|idle]"; runs inside the scope's LightRAG (it has the API key)
  local code='import json,os,urllib.request
h={"X-API-Key":os.environ.get("LIGHTRAG_API_KEY","")}
g=lambda p: json.load(urllib.request.urlopen(urllib.request.Request("http://localhost:9621"+p,headers=h),timeout=10))
c=g("/documents/status_counts")["status_counts"]; s=g("/documents/pipeline_status")
print(c.get("processed",0), c.get("all",0), "busy" if s.get("busy") else "idle", c.get("failed",0))'
  if [ $K8S = 1 ]; then kubectl -n context-stack exec "deploy/lightrag-$1" -- python -c "$code" 2>/dev/null
  else dc exec -T "lightrag-$1" python -c "$code" 2>/dev/null; fi
}
ready() {
  ensure_python; compose_files; ok=1
  head_ "document ingestion per scope"
  for s in $(scopes); do
    out=$(probe "$s" || true); set -- $out
    if [ -z "${1:-}" ]; then say "  $s: not reachable yet"; ok=0; continue; fi
    line="  $s: $1/$2 processed [$3]"; [ "${4:-0}" != 0 ] && line="$line ${4} failed"
    if [ "$2" != 0 ] && [ "$1" = "$2" ] && [ "$3" = idle ]; then say "$line  ${G}ready${N}"; else say "$line"; ok=0; fi
  done
  [ $ok = 1 ] && { say "${G}every scope is ready.${N}"; return 0; }
  warn "not ready yet: answers only cover processed documents. Watch with: scripts/demo.sh watch $K"; return 1
}

late_page_add() {
  grep -q "demo-late-page" test/fixtures/confluence.yaml && { say "the late page is already there"; return 0; }
  python3 - <<'PY'
import pathlib
p = pathlib.Path("test/fixtures/confluence.yaml"); s = p.read_text()
page = '''    # demo-late-page-begin (added by scripts/demo.sh add-page; removed by demo.sh down)
    - id: 1099
      title: "Postmortem: settlement SFTP outage (2026-09-08)"
      version: 1
      ancestors: [Engineering, Postmortems]
      body: "<h1>Postmortem: settlement SFTP outage</h1><p>On 2026-09-08 the acquirer rotated its SFTP host key without notice; the ledger service exhausted its 24 retries (see PAY-REQ-1) and paged on-call at 20:31 UTC. Root cause: the known_hosts file is baked into the ledger image. Action item: move known_hosts to a mounted secret, owner payments-team, due 2026-09-30. Postmortem id LATE-PAGE-0042.</p>"
    # demo-late-page-end
'''
i = s.index("  DOCS:")
p.write_text(s[:i] + page + s[i:]); print("  added page 1099 to the ENG space in test/fixtures/confluence.yaml")
PY
  if [ $K8S = 1 ]; then kubectl apply -k test >/dev/null && say "  ConfigMap updated; the mock picks it up within about a minute"
  else say "  the mock Confluence re-reads the file on its next request"; fi
  say "  Ask the agent about the settlement SFTP outage postmortem: the index misses, the live fallback finds it (LATE-PAGE-0042)."
  say "  Then 'make sync' (or wait for the schedule) and it becomes part of the index."
}
late_page_remove() {
  grep -q "demo-late-page" test/fixtures/confluence.yaml || return 0
  python3 -c "
import pathlib,re; p=pathlib.Path('test/fixtures/confluence.yaml'); s=p.read_text()
p.write_text(re.sub(r'    # demo-late-page-begin.*?# demo-late-page-end\n', '', s, flags=re.S))"
  say "  removed the demo page from test/fixtures/confluence.yaml"
}

# ---------------------------------------------------------------------------------------------- commands
case $CMD in
  help|-h|--help) sed -n 2,18p "$0"; exit 0;;

  personas) personas;;

  up)
    for t in docker openssl curl python3; do command -v $t >/dev/null || { echo "missing: $t" >&2; exit 1; }; done
    [ $K8S = 1 ] && { command -v kubectl >/dev/null || { echo "missing: kubectl" >&2; exit 1; }; }
    bootstrap_env; ensure_python; compose_files
    [ -z "$(env_get GITHUB_TOKEN)" ] && warn "GITHUB_TOKEN is empty: fine for the four public repositories of the demo (unauthenticated GitHub API, rate-limited); required for private ones. See docs/DEMO.md."
    HO=""; grep -q "host.docker.internal" config/stack.env && HO=--host-ollama
    grep -q "host.docker.internal" config/stack.env && pull_models
    head_ "starting the stack with the test environment (scripts/up.sh --test $HO --index --sync $K)"
    PYTHON=$PY scripts/up.sh --test $HO --index --sync $K
    grep -q "host.docker.internal" config/stack.env || pull_models
    head_ "${Y}NOT READY YET.${N}${B} Documents are being extracted into the per-scope graphs; code is being indexed.${N}"
    say "  Watch until every scope reads  N/N processed [idle]  and the code graph shows every repo indexed:"
    say "      ${B}scripts/demo.sh watch $K${N}         (or: scripts/demo.sh ready $K)"
    say "  Then connect an agent:"
    say "      ${B}scripts/demo.sh connect --as alice $K${N}"
    say "  and in a second terminal, while you talk to it:"
    say "      ${B}scripts/demo.sh activity $K${N}"
    say "  Storyline and prompts to try: docs/DEMO.md";;

  ready)  ready;;
  watch)  exec scripts/watch.sh $K;;
  status) exec scripts/status.sh $K;;
  activity) exec python3 scripts/activity.py --mode "$([ $K8S = 1 ] && echo k8s || echo compose)";;
  smoke)
    ensure_python; compose_files
    if [ $K8S = 1 ]; then kubectl -n context-stack port-forward svc/gateway 8090:8090 >/dev/null 2>&1 & PF=$!; sleep 2; fi
    $PY scripts/smoke-test.py --live --activity "$([ $K8S = 1 ] && echo k8s || echo compose)" || true
    [ -n "${PF:-}" ] && kill $PF 2>/dev/null; true;;

  add-page) late_page_add;;

  connect)
    [ -n "$AS" ] || { echo "usage: scripts/demo.sh connect --as <alice|bob|carol|dave> [--k8s] [--write]" >&2; personas; exit 2; }
    groups=$(persona_groups "$AS") || { echo "unknown persona '$AS'" >&2; personas; exit 2; }
    URL=http://127.0.0.1:8090/mcp
    head_ "connect as $AS (groups: ${groups:-none}) to $URL"
    [ $K8S = 1 ] && warn "  Kubernetes: keep a port-forward running in another terminal:  kubectl -n context-stack port-forward svc/gateway 8090:8090"
    say ""
    say "  ${B}Claude Code${N} (user scope: available in every project; --scope project writes .mcp.json instead)"
    say "      claude mcp remove context 2>/dev/null; \\"
    say "      claude mcp add --transport http context $URL --scope user \\"
    say "        --header \"X-Forwarded-User: $AS\" --header \"X-Forwarded-Groups: $groups\""
    say "      claude mcp list        # then in a session: /mcp__context__start_task <task> ... /mcp__context__wrap_up"
    say ""
    say "  ${B}Cursor${N}  (~/.cursor/mcp.json for all projects, .cursor/mcp.json for this one; Settings > MCP shows the tools)"
    say "      { \"mcpServers\": { \"context\": { \"url\": \"$URL\","
    say "          \"headers\": { \"X-Forwarded-User\": \"$AS\", \"X-Forwarded-Groups\": \"$groups\" } } } }"
    say ""
    say "  Switching persona = the same command with another name (scripts/demo.sh personas)."
    if [ $WRITE = 1 ]; then
      mkdir -p .cursor .demo
      printf '{ "mcpServers": { "context": { "type": "http", "url": "%s", "headers": { "X-Forwarded-User": "%s", "X-Forwarded-Groups": "%s" } } } }\n' "$URL" "$AS" "$groups" > .mcp.json
      printf '{ "mcpServers": { "context": { "url": "%s", "headers": { "X-Forwarded-User": "%s", "X-Forwarded-Groups": "%s" } } } }\n' "$URL" "$AS" "$groups" > .cursor/mcp.json
      printf '.mcp.json\n.cursor/mcp.json\n' > .demo/written
      say "  ${G}written${N} .mcp.json (Claude Code, project scope) and .cursor/mcp.json (Cursor) as $AS; both gitignored, removed by 'down'."
      say "  Open this repository in Claude Code or Cursor and the server is there (Claude Code asks once to trust .mcp.json)."
    fi
    say ""; ready || true;;

  down)
    ensure_python; compose_files
    late_page_remove
    if [ -f .demo/written ]; then while read -r f; do [ -f "$f" ] && rm -f "$f" && say "  removed $f"; done < .demo/written; rm -f .demo/written; fi
    head_ "scripts/down.sh $([ $VOLUMES = 1 ] && echo --volumes) $K"
    scripts/down.sh $([ $VOLUMES = 1 ] && echo --volumes) $K
    [ $VOLUMES = 1 ] && say "  data volumes deleted; config/stack.env and .demo/venv kept (delete them by hand if you want a clean slate)"
    say "  done. 'claude mcp remove context' if you added the server at user scope.";;

  *) echo "unknown command '$CMD'" >&2; sed -n 2,18p "$0"; exit 2;;
esac

#!/usr/bin/env bash
# Bring the stack up (Docker Compose by default, Kubernetes with --k8s), with visible progress for every phase.
#
#   scripts/up.sh [--test] [--host-ollama] [--gpu] [--no-build] [--index] [--sync] [--wait] [--k8s]
#
#   --test         add the test environment (mock Confluence / Backstage / Jama, fixture docs)
#   --host-ollama  use an Ollama already running on the host (compose only; k8s: set *_BASE_URL in stack.env)
#   --gpu          NVIDIA override for the Ollama container (compose only)
#   --no-build     skip building the gateway / ingest / codegraph / mock images
#   --index        run the code-graph indexer for every scope after start (skips repos already indexed at that commit)
#   --sync         trigger a full document sync after start
#   --wait         after --sync, stay and show LightRAG processing per scope until every document is processed
#   --k8s          apply k8s/ (or test/ with --test) instead of compose
#
# Phases print as  [hh:mm:ss +Ns] >> phase ; the wait loops print one line per check so a slow step is visibly
# alive. At any time, `scripts/status.sh` (or --watch) shows the same progress; `scripts/status.sh --k8s` on a cluster.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=${PYTHON:-python3}
TEST=0 HOST_OLLAMA=0 GPU=0 BUILD=1 INDEX=0 SYNC=0 WAIT=0 K8S=0
for a in "$@"; do case $a in
  --test) TEST=1;; --host-ollama) HOST_OLLAMA=1;; --gpu) GPU=1;; --no-build) BUILD=0;;
  --index) INDEX=1;; --sync) SYNC=1;; --wait) WAIT=1;; --k8s) K8S=1;; -h|--help) sed -n 2,17p "$0"; exit 0;;
  *) echo "unknown option $a" >&2; exit 2;; esac; done

T0=$(date +%s)
step() { printf '\n[%s +%3ds] >> %s\n' "$(date +%H:%M:%S)" $(( $(date +%s) - T0 )) "$*"; }
note() { printf '[%s +%3ds]    %s\n' "$(date +%H:%M:%S)" $(( $(date +%s) - T0 )) "$*"; }

[ -f config/stack.env ] || { echo "config/stack.env missing: cp config/stack.env.example config/stack.env and edit it" >&2; exit 1; }
export COMPOSE_ENV_FILES="$PWD/config/stack.env"
FILES=(-f docker/compose.yaml -f docker/compose.scopes.yaml)
[ $HOST_OLLAMA = 1 ] && FILES+=(-f docker/compose.host-ollama.yaml)
[ $GPU = 1 ] && FILES+=(-f docker/compose.gpu.yaml)
[ $TEST = 1 ] && FILES+=(-f docker/compose.test.yaml)
dc() { docker compose "${FILES[@]}" "$@"; }
scopes() { $PYTHON -c "import yaml;print(' '.join(yaml.safe_load(open('config/scopes.yaml'))['scopes']))"; }
SECRET=$(grep ^INGEST_WEBHOOK_SECRET config/stack.env | cut -d= -f2)

step "generate docker/compose.scopes.yaml and k8s/generated/ from config/ and plugins/"
$PYTHON scripts/gen-scopes.py compose > docker/compose.scopes.yaml
$PYTHON scripts/gen-scopes.py k8s | sed 's/^/    /'

if [ $BUILD = 1 ]; then
  step "build images (gateway, ingest, codegraph, mocks) - docker prints its own progress"
  dc build 2>&1 | grep -E "Built|ERROR|error:" | sed 's/^/    /'
fi

# ---------------------------------------------------------------------------------------------- kubernetes
if [ $K8S = 1 ]; then
  TARGET=$([ $TEST = 1 ] && echo test || echo k8s)
  step "kubectl apply -k $TARGET"
  kubectl apply -k "$TARGET" | sed 's/^/    /'
  step "wait for pods (namespace context-stack)"
  for i in $(seq 1 120); do
    total=$(kubectl -n context-stack get pods --no-headers 2>/dev/null | wc -l | tr -d ' ')
    ready=$(kubectl -n context-stack get pods --no-headers 2>/dev/null | awk '$2 ~ /^([0-9]+)\/\1$/ && $3=="Running"' | wc -l | tr -d ' ')
    pending=$(kubectl -n context-stack get pods --no-headers 2>/dev/null | awk '!($2 ~ /^([0-9]+)\/\1$/ && $3=="Running") {print $1}' | tr '\n' ' ')
    note "pods ready $ready/$total${pending:+  waiting: $pending}"
    [ "$total" -gt 0 ] && [ "$ready" = "$total" ] && break
    sleep 5
  done
  if [ $INDEX = 1 ]; then
    for s in $(scopes); do
      step "index code for scope $s (job indexer-$s-now)"
      kubectl -n context-stack delete job "indexer-$s-now" --ignore-not-found >/dev/null
      kubectl -n context-stack create job --from="cronjob/indexer-$s" "indexer-$s-now" >/dev/null
      kubectl -n context-stack wait --for=condition=complete "job/indexer-$s-now" --timeout=1800s >/dev/null &
      WPID=$!; while kill -0 $WPID 2>/dev/null; do note "$(kubectl -n context-stack logs job/indexer-$s-now --tail 1 2>/dev/null | cut -c1-100)"; sleep 10; done
      kubectl -n context-stack logs "job/indexer-$s-now" 2>/dev/null | grep -E ">>|Successfully|Error" | sed 's/^/    /'
    done
  fi
  if [ $SYNC = 1 ]; then
    step "sync all document sources (through a temporary port-forward to the ingest)"
    kubectl -n context-stack port-forward svc/ingest 18080:8080 >/dev/null 2>&1 & PF=$!; sleep 3
    curl -fsS -X POST localhost:18080/sync/all -H "X-Ingest-Secret: $SECRET" | sed 's/^/    /'; echo
    kill $PF 2>/dev/null || true
  fi
  step "done. progress: scripts/status.sh --k8s --watch   |   kubectl -n context-stack get pods -w   |   kubectl -n context-stack logs -f deploy/lightrag-<scope>"
  [ $WAIT = 1 ] && exec scripts/status.sh --k8s --watch
  exit 0
fi

# ---------------------------------------------------------------------------------------------- compose
step "docker compose up"
dc up -d --remove-orphans 2>&1 | grep -E "Error|error" | sed 's/^/    /' || true
step "wait for services"
for i in $(seq 1 120); do
  total=$(dc ps --format '{{.Service}}' | wc -l | tr -d ' ')
  up=$(dc ps --format '{{.Service}} {{.Status}}' | { grep -E ' Up' || true; } | wc -l | tr -d ' ')
  waiting=$(dc ps --format '{{.Service}} {{.Status}}' | { grep -vE ' Up' || true; } | awk '{print $1}' | tr '\n' ' ')
  note "services up $up/$total${waiting:+  waiting: $waiting}"
  [ "$total" -gt 0 ] && [ "$up" = "$total" ] && break
  sleep 5
done
if [ $INDEX = 1 ]; then
  for s in $(scopes); do
    step "index code for scope $s (clone + cgc index into falkordb-$s; unchanged repos are skipped)"
    dc --profile jobs run --rm "indexer-$s" 2>&1 | grep -E ">>|Successfully|nodes|Error|error" | sed 's/^/    /'
  done
fi
if [ $SYNC = 1 ]; then
  step "sync all document sources -> per-scope LightRAG (the ingest answers at once; extraction runs in the background)"
  curl -fsS -X POST localhost:8080/sync/all -H "X-Ingest-Secret: $SECRET" | sed 's/^/    /'; echo
  for i in $(seq 1 60); do
    line=$(dc logs --no-log-prefix --since 10m ingest 2>/dev/null | { grep -E "sync all done|Traceback" || true; } | tail -1 | cut -c1-140)
    [ -n "$line" ] && { note "$line"; break; }
    note "ingest: listing sources and handing documents to LightRAG..."; sleep 5
  done
fi
step "done. gateway http://127.0.0.1:8090/mcp | progress: scripts/status.sh --watch | logs: docker compose $(printf -- '%s ' "${FILES[@]}")logs -f lightrag-<scope>"
[ $WAIT = 1 ] && exec scripts/status.sh --watch
exit 0

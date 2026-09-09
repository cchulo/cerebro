#!/usr/bin/env bash
# Bring the stack up (Docker Compose by default, Kubernetes with --k8s).
#
#   scripts/up.sh [--test] [--host-ollama] [--gpu] [--no-build] [--index] [--sync] [--k8s]
#
#   --test         add the test environment (mock Confluence / Backstage / Jama, fixture docs)
#   --host-ollama  use an Ollama already running on the host (compose only; k8s: set *_BASE_URL in stack.env)
#   --gpu          NVIDIA override for the Ollama container (compose only)
#   --no-build     skip building the gateway / ingest / codegraph / mock images
#   --index        run the code-graph indexer for every scope after start
#   --sync         trigger a full document sync after start
#   --k8s          apply k8s/ (or test/ with --test) instead of compose
#
# Regenerates docker/compose.scopes.yaml and k8s/generated/ first (config/scopes.yaml + plugins/ -> infra).
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=${PYTHON:-python3}
TEST=0 HOST_OLLAMA=0 GPU=0 BUILD=1 INDEX=0 SYNC=0 K8S=0
for a in "$@"; do case $a in
  --test) TEST=1;; --host-ollama) HOST_OLLAMA=1;; --gpu) GPU=1;; --no-build) BUILD=0;;
  --index) INDEX=1;; --sync) SYNC=1;; --k8s) K8S=1;; -h|--help) sed -n 2,15p "$0"; exit 0;;
  *) echo "unknown option $a" >&2; exit 2;; esac; done

[ -f config/stack.env ] || { echo "config/stack.env missing: cp config/stack.env.example config/stack.env and edit it" >&2; exit 1; }
export COMPOSE_ENV_FILES="$PWD/config/stack.env"
FILES=(-f docker/compose.yaml -f docker/compose.scopes.yaml)
[ $HOST_OLLAMA = 1 ] && FILES+=(-f docker/compose.host-ollama.yaml)
[ $GPU = 1 ] && FILES+=(-f docker/compose.gpu.yaml)
[ $TEST = 1 ] && FILES+=(-f docker/compose.test.yaml)
dc() { docker compose "${FILES[@]}" "$@"; }
scopes() { $PYTHON -c "import yaml;print(' '.join(yaml.safe_load(open('config/scopes.yaml'))['scopes']))"; }

echo ">> generating docker/compose.scopes.yaml and k8s/generated/"
$PYTHON scripts/gen-scopes.py compose > docker/compose.scopes.yaml
$PYTHON scripts/gen-scopes.py k8s

if [ $BUILD = 1 ]; then
  echo ">> building images"
  dc build
fi

if [ $K8S = 1 ]; then
  echo ">> kubectl apply -k $([ $TEST = 1 ] && echo test || echo k8s)"
  kubectl apply -k "$([ $TEST = 1 ] && echo test || echo k8s)"
  echo ">> waiting for pods"
  kubectl -n context-stack wait --for=condition=Ready pod --all --timeout=600s || true
  kubectl -n context-stack get pods
  if [ $INDEX = 1 ]; then for s in $(scopes); do kubectl -n context-stack create job --from=cronjob/indexer-$s "indexer-$s-$(date +%s)"; done; fi
  if [ $SYNC = 1 ]; then
    kubectl -n context-stack port-forward svc/ingest 18080:8080 >/dev/null 2>&1 & PF=$!; sleep 3
    curl -fsS -X POST localhost:18080/sync/all -H "X-Ingest-Secret: $(grep ^INGEST_WEBHOOK_SECRET config/stack.env | cut -d= -f2)"; echo
    kill $PF
  fi
  exit 0
fi

echo ">> docker compose up"
dc up -d --remove-orphans
echo ">> waiting for services"
for i in $(seq 1 60); do
  notup=$(dc ps --format '{{.Service}} {{.Status}}' | { grep -vE ' Up' || true; } | wc -l | tr -d ' ')   # grep exits 1 when all are up
  [ "$notup" = 0 ] && break; sleep 5
done
dc ps --format 'table {{.Service}}\t{{.Status}}'
if [ $INDEX = 1 ]; then for s in $(scopes); do echo ">> indexing scope $s"; dc --profile jobs run --rm "indexer-$s"; done; fi
if [ $SYNC = 1 ]; then
  echo ">> sync all sources"
  curl -fsS -X POST localhost:8080/sync/all -H "X-Ingest-Secret: $(grep ^INGEST_WEBHOOK_SECRET config/stack.env | cut -d= -f2)"; echo
fi
echo ">> gateway: http://127.0.0.1:8090/mcp   sourcebot: http://127.0.0.1:3000   hindsight UI: http://127.0.0.1:9999"

#!/usr/bin/env bash
# One screen of "is it working / is it progressing", for Docker Compose (default) or Kubernetes (--k8s).
#
#   scripts/status.sh            services, per-scope LightRAG processing, code graph, last sync
#   scripts/status.sh --watch    refresh every 15 s until Ctrl-C
#   scripts/status.sh --k8s      same for the context-stack namespace
#
# What "processed" means: LightRAG has finished LLM extraction for that many documents; answers only cover those.
set -uo pipefail
cd "$(dirname "$0")/.."
K8S=0 WATCH=0
for a in "$@"; do case $a in --k8s) K8S=1;; --watch|-w) WATCH=1;; -h|--help) sed -n 2,9p "$0"; exit 0;; esac; done
[ -f config/stack.env ] && export COMPOSE_ENV_FILES="$PWD/config/stack.env"
FILES=(-f docker/compose.yaml)
[ -f docker/compose.scopes.yaml ] && FILES+=(-f docker/compose.scopes.yaml)
FILES+=(-f docker/compose.host-ollama.yaml -f docker/compose.test.yaml)
SCOPES=$(python3 -c "import yaml;print(' '.join(yaml.safe_load(open('config/scopes.yaml'))['scopes']))" 2>/dev/null \
      || grep -E '^  [a-z0-9_-]+:$' config/scopes.yaml | sed 's/[: ]//g' | tr '\n' ' ')

# python one-liner run INSIDE a lightrag container (it has python + LIGHTRAG_API_KEY); prints one line
LIGHTRAG_PROBE='import json,os,urllib.request
h={"X-API-Key":os.environ.get("LIGHTRAG_API_KEY","")}
def get(p):
    return json.load(urllib.request.urlopen(urllib.request.Request("http://localhost:9621"+p,headers=h),timeout=10))
c=get("/documents/status_counts").get("status_counts",{}); s=get("/documents/pipeline_status")
done=c.get("processed",0); total=c.get("all",0); failed=c.get("failed",0)
state="busy" if s.get("busy") else "idle"
print(f"{done}/{total} processed" + (f", {failed} failed" if failed else "") + f"  [{state}]  " + (s.get("latest_message") or "")[:70])'

compose_status() {
  echo "== services ($(date +%H:%M:%S))"
  docker compose "${FILES[@]}" ps --format '{{.Service}}\t{{.Status}}' 2>/dev/null | sort | awk -F'\t' '{printf "   %-20s %s\n", $1, $2}'
  [ -z "$(docker compose "${FILES[@]}" ps -q 2>/dev/null)" ] && { echo "   (nothing running)"; return; }
  echo "== documents per scope (LightRAG)"
  for s in $SCOPES; do
    printf "   %-10s " "$s"
    docker compose "${FILES[@]}" exec -T "lightrag-$s" python -c "$LIGHTRAG_PROBE" 2>/dev/null || echo "(not reachable yet)"
  done
  echo "== code graph per scope (repositories cloned / indexed)"
  for s in $SCOPES; do
    printf "   %-10s " "$s"
    docker compose "${FILES[@]}" exec -T "codegraph-$s" sh -c 'ls /workspace 2>/dev/null | grep -v "^\." | tr "\n" " "; n=$(ls -a /workspace 2>/dev/null | grep -c "^\.indexed-"); echo "($n indexed)"' 2>/dev/null || echo "(not reachable yet)"
  done
  echo "== ingest: last sync"
  docker compose "${FILES[@]}" logs --no-log-prefix --tail 200 ingest 2>/dev/null | grep -E "sync .* (start|done)|Traceback" | tail -2 | cut -c1-150 | sed 's/^/   /'
  echo "== memory / search / gateway"
  up() { local code; code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 "$@"); case $code in 000) echo "not answering yet";; *) echo "reachable (http $code)";; esac; }
  printf "   hindsight: %s   sourcebot: %s   gateway: %s\n" \
    "$(up http://127.0.0.1:8888/health)" "$(up http://127.0.0.1:3000/)" \
    "$(up -X POST http://127.0.0.1:8090/mcp -H 'Content-Type: application/json' -d '{}')"
}

k8s_status() {
  echo "== pods ($(date +%H:%M:%S))"
  kubectl -n context-stack get pods --no-headers 2>/dev/null | awk '{printf "   %-42s %-8s %s\n", $1, $2, $3}'
  kubectl get ns context-stack >/dev/null 2>&1 || { echo "   (namespace context-stack does not exist)"; return; }
  echo "== documents per scope (LightRAG)"
  for s in $SCOPES; do
    printf "   %-10s " "$s"
    kubectl -n context-stack exec "deploy/lightrag-$s" -- python -c "$LIGHTRAG_PROBE" 2>/dev/null || echo "(not reachable yet)"
  done
  echo "== code graph per scope"
  for s in $SCOPES; do
    printf "   %-10s " "$s"
    kubectl -n context-stack exec "deploy/codegraph-$s" -- sh -c 'ls /workspace 2>/dev/null | grep -v "^\." | tr "\n" " "; n=$(ls -a /workspace 2>/dev/null | grep -c "^\.indexed-"); echo "($n indexed)"' 2>/dev/null || echo "(not reachable yet)"
  done
  echo "== jobs"
  kubectl -n context-stack get jobs --no-headers 2>/dev/null | awk '{printf "   %-40s %s\n", $1, $2}'
  echo "== ingest: last sync"
  kubectl -n context-stack logs deploy/ingest --tail 200 2>/dev/null | grep -E "sync .* (start|done)|Traceback" | tail -2 | cut -c1-150 | sed 's/^/   /'
}

while true; do
  [ $WATCH = 1 ] && clear
  if [ $K8S = 1 ]; then k8s_status; else compose_status; fi
  [ $WATCH = 1 ] || break
  echo; echo "(refreshing every 15 s, Ctrl-C to stop)"; sleep 15
done

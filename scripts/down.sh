#!/usr/bin/env bash
# Tear the stack down (Docker Compose by default, Kubernetes with --k8s, both with --all-targets).
#
#   scripts/down.sh                 stop and remove containers and networks; data volumes are kept
#   scripts/down.sh --volumes       ... and delete every data volume (Postgres, Redis, LightRAG, FalkorDB, repos, state)
#   scripts/down.sh --images        ... and remove the images built by this repo (gateway, ingest, codegraph, mocks)
#   scripts/down.sh --pulled        ... and remove the pulled engine images too (LightRAG, Hindsight, Sourcebot, ...)
#   scripts/down.sh --k8s           tear down the Kubernetes deployment instead (namespace context-stack;
#                                   --volumes deletes its PersistentVolumeClaims and the namespace)
#   scripts/down.sh --all-targets   compose and Kubernetes
#   scripts/down.sh --nuke          = --volumes --images --pulled --all-targets
#
# Nothing here touches config/ or plugins/. Generated files stay; `make gen` rewrites them anyway.
set -euo pipefail
cd "$(dirname "$0")/.."
VOLUMES=0 IMAGES=0 PULLED=0 K8S=0 COMPOSE=1
for a in "$@"; do case $a in
  --volumes|-v) VOLUMES=1;; --images) IMAGES=1;; --pulled) PULLED=1;;
  --k8s) K8S=1; COMPOSE=0;; --all-targets) K8S=1; COMPOSE=1;;
  --nuke) VOLUMES=1; IMAGES=1; PULLED=1; K8S=1; COMPOSE=1;;
  -h|--help) sed -n 2,13p "$0"; exit 0;; *) echo "unknown option $a" >&2; exit 2;; esac; done
PROJECT=agent-context-stack
[ -f config/stack.env ] && export COMPOSE_ENV_FILES="$PWD/config/stack.env"

if [ $COMPOSE = 1 ]; then
  echo ">> compose: removing containers and networks$([ $VOLUMES = 1 ] && echo ' and volumes')"
  FILES=(-f docker/compose.yaml)
  [ -f docker/compose.scopes.yaml ] && FILES+=(-f docker/compose.scopes.yaml)
  FILES+=(-f docker/compose.host-ollama.yaml -f docker/compose.test.yaml)
  docker compose "${FILES[@]}" --profile jobs down --remove-orphans $([ $VOLUMES = 1 ] && echo --volumes) 2>&1 | grep -vE "variable is not set" || true
  # anything else labelled with the project (stale scopes, renamed services, one-off jobs)
  docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" | xargs -r docker rm -f >/dev/null 2>&1 || true
  if [ $VOLUMES = 1 ]; then
    docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT" | xargs -r docker volume rm >/dev/null 2>&1 || true
  fi
  docker network ls -q --filter "label=com.docker.compose.project=$PROJECT" | xargs -r docker network rm >/dev/null 2>&1 || true
fi

if [ $K8S = 1 ] && kubectl get ns context-stack >/dev/null 2>&1; then
  echo ">> kubernetes: deleting workloads in namespace context-stack$([ $VOLUMES = 1 ] && echo ', PVCs and the namespace')"
  if [ $VOLUMES = 1 ]; then
    kubectl delete namespace context-stack --wait=true
  else
    kubectl -n context-stack delete deploy,statefulset,cronjob,job,svc,configmap,secret --all --wait=true
  fi
fi

if [ $IMAGES = 1 ]; then
  echo ">> removing images built by this repo"
  docker images --format '{{.Repository}}:{{.Tag}}' | grep -E "^${PROJECT}-" | xargs -r docker rmi -f >/dev/null 2>&1 || true
fi
if [ $PULLED = 1 ]; then
  echo ">> removing pulled engine images"
  docker images --format '{{.Repository}}:{{.Tag}}' | grep -E "hkuds/lightrag|vectorize-io/hindsight|sourcebot-dev/sourcebot|falkordb/falkordb|pgvector/pgvector|^redis:|ollama/ollama|sooperset/mcp-atlassian" | xargs -r docker rmi -f >/dev/null 2>&1 || true
fi

echo ">> left behind:"
echo "   containers: $(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" | wc -l | tr -d ' ')   volumes: $(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT" | wc -l | tr -d ' ')   networks: $(docker network ls -q --filter "label=com.docker.compose.project=$PROJECT" | wc -l | tr -d ' ')   built images: $(docker images -q --filter "reference=${PROJECT}-*" | wc -l | tr -d ' ')"
kubectl get ns context-stack >/dev/null 2>&1 && echo "   kubernetes: namespace context-stack still exists ($(kubectl -n context-stack get pods --no-headers 2>/dev/null | wc -l | tr -d ' ') pods)" || echo "   kubernetes: no context-stack namespace"

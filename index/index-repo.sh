#!/usr/bin/env bash
# Clone/refresh remote repos into /workspace and (re)index them with CodeGraphContext.
# Usage:
#   index-repo.sh all                       # every repo in $GIT_DOC_REPOS
#   index-repo.sh https://github.com/org/x  # one repo (e.g. from a CI webhook)
# Optionally runs a SCIP indexer if SCIP_CMD is set (e.g. "scip-python index" or "scip-typescript index").
# Pull model: the job checks upstream itself and skips repos whose HEAD has not moved since the last index
# (FORCE=1 re-indexes regardless), so it is safe to schedule hourly.
set -euo pipefail
WORKSPACE=${WORKSPACE:-/workspace}
mkdir -p "$WORKSPACE"

auth_url() {
  # inject token for private GitHub repos
  local url=$1
  if [[ -n "${GITHUB_TOKEN:-}" && "$url" == https://github.com/* ]]; then
    echo "https://x-access-token:${GITHUB_TOKEN}@${url#https://}"
  else
    echo "$url"
  fi
}

index_one() {
  local url=$1
  local name; name=$(basename "${url%.git}")
  local dir="$WORKSPACE/$name"
  local stamp="$WORKSPACE/.indexed-$name"        # commit last indexed; lets the job run often and do nothing
  if [[ -d "$dir/.git" ]]; then
    git -C "$dir" remote set-url origin "$(auth_url "$url")"
    git -C "$dir" fetch --depth 1 origin
    git -C "$dir" reset --hard FETCH_HEAD
  else
    git clone --depth 1 "$(auth_url "$url")" "$dir"
  fi
  local head; head=$(git -C "$dir" rev-parse HEAD)
  if [[ "${FORCE:-}" != "1" && -f "$stamp" && "$(cat "$stamp")" == "$head" ]]; then
    echo ">> $name unchanged at ${head:0:12}; skipping"
    return
  fi
  echo ">> indexing $name at ${head:0:12}"
  ( cd "$dir" && cgc index . )                    # CodeGraphContext graph (FalkorDB)
  echo "$head" > "$stamp"
  if [[ -n "${SCIP_CMD:-}" ]]; then
    ( cd "$dir" && $SCIP_CMD ) || echo "SCIP indexing failed for $name (non-fatal)"
  fi
}

if [[ "${1:-all}" == "all" ]]; then
  IFS=',' read -ra REPOS <<< "${GIT_DOC_REPOS:-}"
  for r in "${REPOS[@]}"; do [[ -n "$r" ]] && index_one "$r"; done
else
  index_one "$1"
fi

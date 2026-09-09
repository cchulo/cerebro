#!/usr/bin/env bash
# Clone/refresh remote repos into /workspace and (re)index them with CodeGraphContext.
# Usage:
#   index-repo.sh all                       # every repo in $GIT_DOC_REPOS
#   index-repo.sh https://github.com/org/x  # one repo (e.g. from a CI webhook)
# Optionally runs a SCIP indexer if SCIP_CMD is set (e.g. "scip-python index" or "scip-typescript index").
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
  if [[ -d "$dir/.git" ]]; then
    git -C "$dir" remote set-url origin "$(auth_url "$url")"
    git -C "$dir" fetch --depth 1 origin
    git -C "$dir" reset --hard FETCH_HEAD
  else
    git clone --depth 1 "$(auth_url "$url")" "$dir"
  fi
  echo ">> indexing $name"
  ( cd "$dir" && cgc index . )                    # CodeGraphContext graph (FalkorDB)
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

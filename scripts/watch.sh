#!/usr/bin/env bash
# Keep the status screen refreshing until Ctrl-C.   scripts/watch.sh [--k8s] [--interval N]   (make watch / make k8s-watch)
exec "$(dirname "$0")/status.sh" --watch "$@"

#!/usr/bin/env python3
"""A fake `tokensave` for indexer tests. Records every invocation as `<cwd>|<args>` in $TOKENSAVE_LOG and mimics
the on-disk effects that matter: `init` creates .tokensave/, `branch add` tracks the checked-out branch in
branch-meta.json (idempotent), `branch remove <b>` untracks it, `sync` touches a marker."""
import json, os, pathlib, subprocess, sys

args = sys.argv[1:]
cwd = pathlib.Path.cwd()
with open(os.environ["TOKENSAVE_LOG"], "a") as f:
    f.write(f"{cwd}|{' '.join(args)}\n")

if args[:1] == ["--version"]:
    print("tokensave 0.0-shim"); sys.exit(0)
if os.environ.get("TOKENSAVE_SHIM_FAIL") and os.environ["TOKENSAVE_SHIM_FAIL"] in " ".join(args):
    print("shim failure", file=sys.stderr); sys.exit(2)

target = cwd
if args[:1] == ["init"] and len(args) > 1 and not args[-1].startswith("--"):
    target = pathlib.Path(args[-1])
ts = target / ".tokensave"
meta = ts / "branch-meta.json"


def load():
    return json.loads(meta.read_text()) if meta.exists() else None


def current_branch():
    return subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=cwd, capture_output=True, text=True).stdout.strip()


if args[:1] == ["init"]:
    ts.mkdir(exist_ok=True)
    (ts / "config.json").write_text(json.dumps({"root_dir": str(target)}))
    (ts / "tokensave.db").write_text("db")
    print("indexing done")
elif args[:1] == ["sync"]:
    if not ts.exists():
        print("not initialized", file=sys.stderr); sys.exit(1)
    (ts / ("synced-" + current_branch().replace("/", "_"))).write_text("")
    print("sync done")
elif args[:2] == ["branch", "add"]:
    b = current_branch()
    head = subprocess.run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=cwd, capture_output=True, text=True).stdout.strip()
    default = head.removeprefix("origin/") or "main"           # the real binary detects the default branch from git
    data = load() or {"default_branch": default, "branches": {default: {"db_file": "tokensave.db"}}}
    if b in data["branches"]:
        print(f"Branch '{b}' is already tracked.")
    else:
        data["branches"][b] = {"db_file": f"branches/{b.replace('/', '_')}.db"}
        print(f"branch '{b}' tracked")
    meta.write_text(json.dumps(data))
elif args[:2] == ["branch", "remove"]:
    data = load() or {"branches": {}}
    data["branches"].pop(args[2], None)
    meta.write_text(json.dumps(data))
else:
    print(f"shim: unsupported {args}", file=sys.stderr); sys.exit(3)

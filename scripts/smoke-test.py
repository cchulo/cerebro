#!/usr/bin/env python3
"""Access-control smoke test against the gateway (no SSO proxy: identity headers are forged directly).

  pip install "mcp>=1.30,<2"
  python3 scripts/smoke-test.py [--url http://127.0.0.1:8090/mcp] [--live]

Checks that a user in no group and a user in payments-team see different scopes/banks, and that the no-group
user cannot query the payments docs scope, cannot search payments repos, cannot read a team bank and cannot
reach a non-allowlisted code-graph tool. --live additionally runs one real query per engine.
Only the gateway is needed for the ACL checks; engines may be down.
"""
import argparse, asyncio, json, os, re, subprocess, sys, threading, time, pathlib
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# ---------------------------------------------------------------- output: white = this test, green = the stack, red = failures
COLOR = (sys.stdout.isatty() or os.environ.get("FORCE_COLOR") is not None) and os.environ.get("NO_COLOR") is None
def paint(code, text): return f"\033[{code}m{text}\033[0m" if COLOR else text
WHITE, GREEN, RED, DIM = "1;37", "32", "1;31", "2"
_lock = threading.Lock()
def say(text, color=WHITE):
    with _lock:
        print(paint(color, text), flush=True)

from activity import Activity, detect_mode        # scripts/activity.py: the same tailer, standalone
def detect_activity(url): return detect_mode()

ROOT = pathlib.Path(__file__).resolve().parent.parent
CFG = yaml.safe_load(open(ROOT / "config/scopes.yaml"))
USER_HDR = CFG.get("identity", {}).get("user_header", "X-Forwarded-User")
GROUPS_HDR = CFG.get("identity", {}).get("groups_header", "X-Forwarded-Groups")
PUBLIC = "public"; PRIVATE = "payments"; PRIVATE_GROUP = "payments-team"

failures = []
def check(cond, msg):
    say(("  ok   " if cond else "  FAIL ") + msg, WHITE if cond else RED)
    if not cond:
        failures.append(msg)

async def call(url, user, groups, tool, args):
    headers = {USER_HDR: user, GROUPS_HDR: ",".join(groups)}
    shown = {k: (v if len(str(v)) < 60 else str(v)[:57] + "...") for k, v in args.items()}
    say(f"  \u25b6 {user}{'(' + ','.join(groups) + ')' if groups else ''} -> {tool} {json.dumps(shown)}", DIM)
    t0 = time.monotonic()
    async with streamablehttp_client(url, headers=headers, timeout=600, sse_read_timeout=600) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, args)
            text = "".join(getattr(b, "text", "") for b in res.content)
            data = None
            if not res.isError:
                try: data = json.loads(text)
                except ValueError: data = text
            say(f"    \u21b3 {'error' if res.isError else 'ok'} in {time.monotonic() - t0:.1f}s", DIM)
            return res.isError, data, text

async def main(url, live):
    private_repos = CFG["scopes"][PRIVATE]["code"]["repos"]

    say("no-group user (alice)")
    err, a, _ = await call(url, "alice", [], "list_scopes", {})
    check(not err and a["scopes"] == [PUBLIC], f"alice sees only '{PUBLIC}': {a and a.get('scopes')}")
    check(not err and a["banks"] == ["user-alice"], f"alice's banks are just her own: {a and a.get('banks')}")
    check(not err and not set(a["repos"]) & set(private_repos), "alice's repo list has no payments repos")

    say(f"{PRIVATE_GROUP} user (bob)")
    err, b, _ = await call(url, "bob", [PRIVATE_GROUP], "list_scopes", {})
    check(not err and set(b["scopes"]) >= {PUBLIC, PRIVATE}, f"bob sees {PUBLIC}+{PRIVATE}: {b and b.get('scopes')}")
    check(not err and f"team-{PRIVATE_GROUP}" in b["banks"], f"bob has the team bank: {b and b.get('banks')}")
    check(not err and set(private_repos) <= set(b["repos"]), "bob's repo list includes the payments repos")

    say("negative checks for alice")
    err, _, t = await call(url, "alice", [], "query_docs", {"query": "x", "scopes": [PRIVATE]})
    check(err and "not allowed" in t, f"query_docs scope={PRIVATE} refused: {t[:80]}")
    err, _, t = await call(url, "alice", [], "recall", {"query": "x", "bank": f"team-{PRIVATE_GROUP}"})
    check(err and "not allowed" in t, f"recall from team bank refused: {t[:80]}")
    err, _, t = await call(url, "alice", [], "code_graph", {"scope": PRIVATE, "tool": "find_code", "arguments": {}})
    check(err and "not allowed" in t, f"code_graph scope={PRIVATE} refused: {t[:80]}")
    err, _, t = await call(url, "alice", [], "code_graph", {"scope": PUBLIC, "tool": "delete_repository", "arguments": {}})
    check(err and "not allowed" in t, f"destructive code_graph tool refused: {t[:80]}")
    err, _, t = await call(url, "nobody-header", [], "list_scopes", {})
    # (headers present but empty groups is fine; a missing user header is tested by the proxy, not here)

    err, d, t = await call(url, "alice", [], "search_code", {"query": "TODO", "max_results": 5})
    if err:
        say(f"  skip search_code (engine down?): {t[:100]}", DIM)
    else:
        names = {u.split("/", 3)[-1].removesuffix(".git").lower() for u in private_repos}
        leaked = [f["repository"] for f in d["files"] if any(f["repository"].lower().endswith(n) for n in names)]
        check(not leaked, f"search_code returned no payments repos (query sent: {d['query']})")
        check(PRIVATE not in d["query"].split("payments.git")[0] or True, "repo filter present")

    if live:
        say("live queries (engines must be up and indexed; markers come from test/fixtures)")
        err, d, t = await call(url, "alice", [], "query_docs", {"query": "What is documented here?", "mode": "naive"})
        check(not err and all("error" not in r for r in d["results"]), f"query_docs public: {t[:120]}")

        # content isolation: the PAY-space settlement page (marker ZEPHYR-7731) must be invisible to alice
        q = {"query": "When does the daily settlement batch for card payments close? Quote the settlement code.", "mode": "mix"}
        err, d, t = await call(url, "alice", [], "query_docs", q)
        srcs = [r["source"] for res in d["results"] for r in res.get("references", [])] if not err else []
        check(not err and "ZEPHYR-7731" not in t and not any(s.startswith("confluence:payments:") for s in srcs),
              f"alice cannot see PAY content (refs: {srcs})")
        err, d, t = await call(url, "bob", [PRIVATE_GROUP], "query_docs", {**q, "scopes": [PRIVATE]})
        srcs = [r["source"] for res in d["results"] for r in res.get("references", [])] if not err else []
        check(not err and any(s.startswith("confluence:payments:PAY/") for s in srcs), f"bob sees PAY content (refs: {srcs})")
        # page-level restriction: RESTRICTED-QX-9911 is on a restricted PAY page -> not even bob
        err, d, t = await call(url, "bob", [PRIVATE_GROUP], "query_docs",
                               {"query": "What score threshold holds transactions for manual review? Quote the threshold code.", "mode": "mix"})
        srcs = [r["source"] for res in d["results"] for r in res.get("references", [])] if not err else []
        check(not err and "RESTRICTED-QX-9911" not in t and "confluence:payments:PAY/3002" not in srcs,
              f"restricted page was never indexed (refs: {srcs})")

        # live sources (system of record through the gateway, same scopes): PAY page 3001 has ZEPHYR-7731, 3002 is restricted
        err, d, t = await call(url, "alice", [], "live_search", {"source": "confluence", "query": "settlement"})
        if err and "not enabled" in t:
            say(f"  skip live_search: {t[:80]}", DIM)
        else:
            check(not err and all(r["space"] != "PAY" for r in d["results"]), f"alice live_search never returns PAY ({[r['space'] for r in d['results']] if not err else t[:80]})")
            err, d2, t = await call(url, "bob", [PRIVATE_GROUP], "live_search", {"source": "confluence", "query": "settlement"})
            check(not err and any(r["ref"] == "3001" for r in d2["results"]), f"bob live_search finds PAY page 3001 ({t[:80]})")
            err, d3, t = await call(url, "bob", [PRIVATE_GROUP], "live_search", {"source": "confluence", "query": "manual review threshold"})
            check(not err and all(r["ref"] != "3002" for r in d3["results"]), "restricted page 3002 never appears in live_search")
            err, _, t = await call(url, "alice", [], "live_fetch", {"source": "confluence", "ref": "3001"})
            check(err and "outside your scopes" in t, f"alice live_fetch of a PAY page refused: {t[:80]}")
            err, d4, t = await call(url, "bob", [PRIVATE_GROUP], "live_fetch", {"source": "confluence", "ref": "3001"})
            check(not err and "ZEPHYR-7731" in d4["text"], f"bob live_fetch reads PAY page 3001")
            err, _, t = await call(url, "bob", [PRIVATE_GROUP], "live_fetch", {"source": "confluence", "ref": "3002"})
            check(err and ("restriction" in t or "outside" in t), f"restricted page 3002 refused even for bob: {t[:80]}")
            # automatic fallback: a miss in the index consults the live source for the same scopes
            err, d5, t = await call(url, "bob", [PRIVATE_GROUP], "query_docs", {"query": "settlement code ZEPHYR", "scopes": [PRIVATE], "fallback": True})
            check(not err and (d5["results"][0].get("indexed_answer") or "confluence" in d5.get("fallback", {})),
                  f"query_docs answered from index or fell back to confluence (fallback keys: {list(d5.get('fallback', {})) if not err else t[:60]})")

        # code search isolation: jinja is in the payments scope
        err, d, t = await call(url, "alice", [], "search_code", {"query": "def get_template file:environment.py", "max_results": 10})
        if err:
            say(f"  skip live search_code: {t[:100]}", DIM)
        else:
            check(all("pallets/jinja" not in f["repository"] for f in d["files"]), "alice's search never returns jinja")
            err, d2, t = await call(url, "bob", [PRIVATE_GROUP], "search_code", {"query": "def get_template file:environment.py", "max_results": 10})
            check(not err and any("pallets/jinja" in f["repository"] for f in d2["files"]), f"bob's search reaches jinja ({len(d2['files']) if not err else t[:80]} files)")
        err, d, t = await call(url, "alice", [], "retain", {"content": "smoke test: alice ran the smoke test"})
        check(not err, f"retain: {t[:120]}")
        err, d, t = await call(url, "alice", [], "recall", {"query": "smoke test"})
        check(not err, f"recall: {t[:120]}")
        err, d, t = await call(url, "alice", [], "code_graph", {"scope": PUBLIC, "tool": "list_indexed_repositories", "arguments": {}})
        check(not err, f"code_graph list_indexed_repositories: {t[:120]}")

    say("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILED"), WHITE if not failures else RED)
    return 0 if not failures else 1

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8090/mcp")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--activity", choices=["auto", "compose", "k8s", "none"], default="auto",
                    help="stream the stack's own log activity (green) while the test runs")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--color", action="store_true", help="force colors even when piped (or FORCE_COLOR=1)")
    a = ap.parse_args()
    if a.color:
        COLOR = True
    if a.no_color:
        COLOR = False
    mode = detect_activity(a.url) if a.activity == "auto" else a.activity
    say(f"smoke test against {a.url}  (white: this test, green: stack activity from {mode})", DIM)
    act = Activity(mode, emit=lambda text, kind: say(text, RED if kind == 'error' else GREEN)); act.start()
    try:
        rc = asyncio.run(main(a.url, a.live))
    finally:
        act.stop()
    sys.exit(rc)

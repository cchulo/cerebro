#!/usr/bin/env python3
"""Live activity of the stack while an agent talks to it: gateway tool calls (who, which tool, how long) in white,
what the engines do in response (LightRAG queries and extraction, Hindsight recall/retain, Sourcebot, mcp-atlassian,
ingest) in green. Ctrl-C to stop.

  scripts/activity.py [--mode auto|compose|k8s] [--all]       (--all: every log line, unfiltered)

Used by scripts/demo.sh activity and, embedded, by scripts/smoke-test.py.
"""
import argparse, os, pathlib, re, subprocess, sys, threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
COLOR = (sys.stdout.isatty() or os.environ.get("FORCE_COLOR") is not None) and os.environ.get("NO_COLOR") is None
def paint(code, text): return f"\033[{code}m{text}\033[0m" if COLOR else text
WHITE, GREEN, RED, DIM = "1;37", "32", "1;31", "2"

class Activity:
    """Tails the stack's logs and hands the interesting lines to `emit(text, kind)`; kind is gateway | engine | error."""
    PATTERN = re.compile(r"call |done |fail |CallToolRequest|POST /|query|Query|Processing|extract|Extract|Merging|embedding|"
                         r"recall|retain|consolidat|reflect|confluence_search|confluence_get_page|search|sync |Cloning|indexing|"
                         r"Indexed|Error|error|Traceback", re.I)
    NOISE = re.compile(r"WORKER_STATS|pipeline_status|/health|status_counts|Terminating session|GET /api/version|Processing request of type|"
                       r"POST /mcp HTTP|GET /rest/api/content/\d+|/documents/paginated|kube-probe", re.I)
    GATEWAY = re.compile(r"^(call|done|fail) ")

    def __init__(self, mode, emit=None, everything=False):
        self.mode, self.proc, self.thread, self.everything = mode, None, None, everything
        self.emit = emit or self._default_emit

    @staticmethod
    def _default_emit(text, kind):
        print(paint({"gateway": WHITE, "error": RED}.get(kind, GREEN), text), flush=True)

    def start(self):
        if self.mode == "compose":
            files = ["-f", "docker/compose.yaml"]
            for f in ("docker/compose.scopes.yaml", "docker/compose.host-ollama.yaml", "docker/compose.test.yaml"):
                if (ROOT / f).exists():
                    files += ["-f", f]
            env = {**os.environ, "COMPOSE_ENV_FILES": str(ROOT / "config/stack.env")}
            cmd = ["docker", "compose", *files, "logs", "-f", "--since", "1s", "--no-color"]
        elif self.mode == "k8s":
            env = os.environ
            cmd = ["kubectl", "-n", "context-stack", "logs", "-f", "--since=1s", "--prefix", "--max-log-requests=40",
                   "-l", "app.kubernetes.io/part-of=context-stack"]
        else:
            return
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env, cwd=ROOT)
        self.thread = threading.Thread(target=self._pump, daemon=True); self.thread.start()

    def _pump(self):
        for line in self.proc.stdout:
            line = line.rstrip()
            if not self.everything and (not self.PATTERN.search(line) or self.NOISE.search(line)):
                continue
            if self.mode == "compose" and "|" in line:
                svc, _, rest = line.partition("|"); svc = svc.strip().replace("agent-context-stack-", "").rsplit("-1", 1)[0]
            elif self.mode == "k8s" and "]" in line:
                svc, _, rest = line.partition("]"); svc = svc.strip("[ ").split("/")[-1].rsplit("-", 2)[0]
            else:
                svc, rest = "stack", line
            rest = re.sub(r"^\s*(INFO|WARNING|ERROR)[:\s-]*", "", rest.strip())
            kind = "gateway" if svc == "gateway" and self.GATEWAY.match(rest) else \
                   "error" if re.search(r"Traceback|ERROR|\berror\b", rest) else "engine"
            self.emit(f"    {svc:<18} {rest[:160]}", kind)

    def stop(self):
        if self.proc:
            self.proc.terminate()

def detect_mode():
    """compose if the gateway container runs, k8s if the gateway deployment exists, else none."""
    if os.environ.get("STACK_ACTIVITY"):
        return os.environ["STACK_ACTIVITY"]
    try:
        out = subprocess.run(["docker", "compose", "-f", "docker/compose.yaml", "ps", "-q", "gateway"], capture_output=True, text=True,
                             cwd=ROOT, env={**os.environ, "COMPOSE_ENV_FILES": str(ROOT / "config/stack.env")}, timeout=15).stdout.strip()
        if out:
            return "compose"
    except Exception:
        pass
    try:
        if subprocess.run(["kubectl", "-n", "context-stack", "get", "deploy/gateway"], capture_output=True, timeout=15).returncode == 0:
            return "k8s"
    except Exception:
        pass
    return "none"

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default="auto", choices=["auto", "compose", "k8s"])
    ap.add_argument("--all", action="store_true", help="show every log line, not only the interesting ones")
    a = ap.parse_args()
    mode = detect_mode() if a.mode == "auto" else a.mode
    if mode == "none":
        sys.exit("no running stack found (neither the compose gateway container nor the k8s gateway deployment)")
    print(paint(DIM, f"tailing the {mode} stack: white = gateway tool calls, green = engines, red = errors. Ctrl-C to stop."))
    act = Activity(mode, everything=a.all); act.start()
    try:
        act.thread.join()
    except KeyboardInterrupt:
        act.stop(); print()

if __name__ == "__main__":
    main()

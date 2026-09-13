"""The bridge's own `search` capability: ripgrep on the checked-out (default) branch, `git grep` on any other
indexed branch. TokenSave has no regex text search (its own PreToolUse hook passes regex patterns through to grep;
README "PreToolUse hook"), so this is what makes a TokenSave unit satisfy `search`.

Branch rule: a branch other than the default is searched with `git grep <pattern> <branch>` so no checkout moves
under the running engine; only branches the indexer recorded (cerebro.bridge.workspace.RepoState.branches) are
accepted, the same set the graph tools accept, so `search` and `graph` agree on what a branch is.
"""
from __future__ import annotations
import asyncio, json, os, shutil
from typing import Any
from .workspace import RepoEntry, Workspace

LANGUAGES = {".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript",
             ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".c": "c", ".h": "c", ".cpp": "cpp",
             ".hpp": "cpp", ".cs": "csharp", ".rb": "ruby", ".php": "php", ".swift": "swift", ".scala": "scala",
             ".sh": "shell", ".yaml": "yaml", ".yml": "yaml", ".json": "json", ".md": "markdown", ".toml": "toml",
             ".sql": "sql", ".html": "html", ".css": "css", ".tf": "terraform", ".proto": "protobuf"}
MAX_RESULTS_CAP = 200


def language_of(path: str) -> str | None:
    return LANGUAGES.get(os.path.splitext(path)[1].lower())


def rg_binary() -> str | None:
    return os.environ.get("RG_BIN") or shutil.which("rg")


async def grep(ws: Workspace, query: str, *, repos: list[str] | None = None, branch: str | None = None,
               regex: bool = False, max_results: int = 20, context: int = 1) -> dict[str, Any]:
    if not query:
        raise ValueError("query must not be empty")
    max_results = max(1, min(int(max_results), MAX_RESULTS_CAP))
    context = max(0, min(int(context), 10))
    hits: list[dict[str, Any]] = []
    errors: list[str] = []
    for repo in ws.select(repos):
        if len(hits) >= max_results:
            break
        if not (ws.path(repo) / ".git").exists():
            errors.append(f"{repo.name}: not checked out yet")
            continue
        try:
            ref = ws.resolve_branch(repo, branch)
        except ValueError as e:
            errors.append(str(e))
            continue
        want = max_results - len(hits)
        if ref is None:
            found = await _rg(ws, repo, query, regex, want, context)
        else:
            found = await _git_grep(ws, repo, ref, query, regex, want, context)
        hits.extend(found)
    return {"hits": hits[:max_results], "errors": errors, "query": query, "regex": regex,
            "branch": branch, "truncated": len(hits) >= max_results}


def _hit(repo: RepoEntry, branch: str | None, path: str, line: int, content: str) -> dict[str, Any]:
    return {"repository": repo.name, "path": path, "line": line, "content": content.rstrip("\n"),
            "language": language_of(path), "branch": branch}


async def _run(cmd: list[str], cwd: str) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    if proc.returncode not in (0, 1):                      # 1 = no matches for both rg and git grep
        raise RuntimeError(f"{cmd[0]} failed ({proc.returncode}): {err.decode(errors='replace')[:400]}")
    return proc.returncode or 0, out


async def _rg(ws: Workspace, repo: RepoEntry, query: str, regex: bool, want: int, context: int) -> list[dict]:
    rg = rg_binary()
    if not rg:
        raise RuntimeError("ripgrep (rg) is not installed in this unit image")
    cmd = [rg, "--json", "--no-config", "--max-count", str(want), "--max-filesize", "2M",
           "--glob", "!.tokensave/**", "--glob", "!.git/**"]
    if context:
        cmd += ["-C", str(context)]
    cmd += ["-F"] if not regex else []
    cmd += ["-e", query, "."]
    _, out = await _run(cmd, str(ws.path(repo)))
    return _parse_rg(out, repo, None, want, context)


def _parse_rg(out: bytes, repo: RepoEntry, branch: str | None, want: int, context: int) -> list[dict]:
    """rg --json: group `context` events around each `match` of the same file into one snippet."""
    hits: list[dict] = []
    before: list[str] = []
    after_slot: dict | None = None
    after_left = 0
    for raw in out.splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        kind, data = ev.get("type"), ev.get("data", {})
        if kind in ("begin", "end"):
            before, after_slot, after_left = [], None, 0
            continue
        text = (data.get("lines", {}) or {}).get("text", "")
        if kind == "context":
            if after_slot is not None and after_left > 0:
                after_slot["_after"].append(text.rstrip("\n"))
                after_left -= 1
                if after_left == 0:
                    after_slot = None
            else:
                before.append(text.rstrip("\n"))
                before = before[-context:] if context else []
        elif kind == "match":
            path = (data.get("path", {}) or {}).get("text", "")
            path = path[2:] if path.startswith("./") else path
            h = _hit(repo, branch, path, int(data.get("line_number") or 0), text)
            h["_before"], h["_after"] = list(before), []
            hits.append(h)
            before, after_slot, after_left = [], h, context
            if len(hits) >= want:
                break
    for h in hits:
        b, a = h.pop("_before"), h.pop("_after")
        if b or a:
            h["content"] = "\n".join(b + [h["content"]] + a)
    return hits


async def _git_grep(ws: Workspace, repo: RepoEntry, ref: str, query: str, regex: bool, want: int,
                    context: int) -> list[dict]:
    """`git grep -z` on a ref prints `<ref>:<path>\\0<line>\\0<text>` per match. NUL is also what separates context
    lines under -z (no `:` / `-` distinction), so context is taken from `git show <ref>:<path>` afterwards."""
    cwd = str(ws.path(repo))
    cmd = ["git", "-C", cwd, "grep", "-I", "-n", "-z", "--no-color", "--full-name", "-E" if regex else "-F",
           "-e", query, ref, "--", "."]
    _, out = await _run(cmd, cwd)
    hits: list[dict] = []
    prefix = (ref + ":").encode()
    for line in out.split(b"\n"):
        if not line:
            continue
        if line.startswith(prefix):
            line = line[len(prefix):]
        parts = line.split(b"\0", 2)
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        hits.append(_hit(repo, ref, parts[0].decode(errors="replace"), int(parts[1]), parts[2].decode(errors="replace")))
        if len(hits) >= want:
            break
    if context and hits:
        files: dict[str, list[str]] = {}
        for h in hits:
            if h["path"] not in files:
                _, blob = await _run(["git", "-C", cwd, "show", f"{ref}:{h['path']}"], cwd)
                files[h["path"]] = blob.decode(errors="replace").splitlines()
            lines = files[h["path"]]
            lo, hi = max(0, h["line"] - 1 - context), min(len(lines), h["line"] + context)
            h["content"] = "\n".join(lines[lo:hi])
    return hits

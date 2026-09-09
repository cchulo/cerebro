"""Git docs sync: shallow-clone each repo, ingest matching Markdown files (README, docs/, ADRs, runbooks).
Source code is deliberately NOT ingested here; that is the code-intelligence layer's job."""
import hashlib, os, subprocess, tempfile
from pathlib import Path
from . import state, lightrag, scopes
from .config import GIT_DOC_GLOBS, GITHUB_TOKEN

def _auth(url: str) -> str:
    if GITHUB_TOKEN and url.startswith("https://github.com/"):
        return url.replace("https://", f"https://x-access-token:{GITHUB_TOKEN}@", 1)
    return url

def _files(root: Path):
    for pattern in GIT_DOC_GLOBS:
        yield from root.glob(pattern)

def sync_repo(url: str) -> dict:
    name = url.rstrip("/").removesuffix(".git").split("/")[-1]
    scope = scopes.scope_for_repo(url)
    if not scope:
        return {"repo": name, "skipped": "repo not listed in any scope"}
    batch, new_state, drop = lightrag.Batch(scope), {}, set()
    changed, seen = 0, set()
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "clone", "--depth", "1", "--quiet", _auth(url), tmp], check=True)
        root = Path(tmp)
        for f in _files(root):
            if not f.is_file():
                continue
            rel = f.relative_to(root).as_posix()
            key = f"git:{name}:{rel}"
            seen.add(key)
            text = f.read_text(errors="ignore")
            version = hashlib.sha256(text.encode()).hexdigest()[:16]
            if state.get(key) == version:
                continue
            header = f"Repository: {name}\nFile: {rel}\nURL: {url}\n\n"
            batch.upsert(key, header + text, title=f"{name}/{rel}")
            new_state[key] = version; changed += 1
    removed = 0
    for key in state.keys_with_prefix(f"git:{name}:"):
        if key not in seen:
            batch.delete(key); drop.add(key); removed += 1
    flushed = batch.flush()
    state.commit(new_state, drop)
    return {"repo": name, "scope": scope, "changed": changed, "removed": removed, "lightrag": flushed}

def sync(repos: list[str] | None = None) -> dict:
    return {"repos": [sync_repo(u) for u in (repos or [r for r, _ in scopes.all_repos()])]}

"""Git docs adapter: shallow-clone each of the scope's repos and ingest matching Markdown/text files
(README, docs/, ADRs, runbooks). Source code is deliberately NOT ingested; that is the code layer's job.

Scope config:  docs: { git: {} }                                  uses the scope's `repos`
               docs: { git: { globs: ["README.md", "docs/**/*.md"] } }
Env:           GIT_DOC_GLOBS (default globs), GITHUB_TOKEN (private GitHub repos)
Webhook filter: {"repo": "https://github.com/org/x.git"}
"""
import hashlib, subprocess, tempfile
from pathlib import Path
from ..config import GIT_DOC_GLOBS, GITHUB_TOKEN
from .base import Source, Document, ScopeContext


def repo_name(url: str) -> str:
    return url.rstrip("/").removesuffix(".git").split("/")[-1]


def _auth(url: str) -> str:
    if GITHUB_TOKEN and url.startswith("https://github.com/"):
        return url.replace("https://", f"https://x-access-token:{GITHUB_TOKEN}@", 1)
    return url


class GitDocsSource(Source):
    name = "git"

    def documents(self, ctx: ScopeContext, filter: dict | None = None):
        globs = ctx.config.get("globs") or GIT_DOC_GLOBS
        repos = ctx.repos
        if filter and filter.get("repo"):
            want = filter["repo"].rstrip("/").removesuffix(".git").lower()
            repos = [r for r in repos if r.rstrip("/").removesuffix(".git").lower() == want]
        for url in repos:
            name = repo_name(url)
            with tempfile.TemporaryDirectory() as tmp:
                subprocess.run(["git", "clone", "--depth", "1", "--quiet", _auth(url), tmp], check=True)
                root = Path(tmp)
                for pattern in globs:
                    for f in sorted(root.glob(pattern)):
                        if not f.is_file():
                            continue
                        rel = f.relative_to(root).as_posix()
                        text = f.read_text(errors="ignore")
                        header = f"Repository: {name}\nFile: {rel}\nURL: {url}\n\n"
                        yield Document(key=f"{name}/{rel}", version=hashlib.sha256(text.encode()).hexdigest()[:16],
                                       text=header + text, title=f"{name}/{rel}")

    def covers(self, key: str, filter: dict | None) -> bool:
        if not filter:
            return True
        return bool(filter.get("repo")) and key.startswith(repo_name(filter["repo"]) + "/")

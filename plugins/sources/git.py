"""Git docs adapter: shallow-clone each of the scope's repos and ingest matching Markdown/text files
(README, docs/, ADRs, runbooks). Source code is deliberately NOT ingested; that is the code layer's job.

Scope config:   docs: { git: {} }                                  uses the scope's `repos`
                docs: { git: { globs: ["README.md", "docs/**/*.md"] } }
Env:            GIT_DOC_GLOBS (default globs, comma-separated), GITHUB_TOKEN (private GitHub repos)
sources: option git: { globs: [...] }
Webhook filter: {"repo": "https://github.com/org/x.git"}
"""
import hashlib, os, subprocess, tempfile
from pathlib import Path
from ingest.sources import Source, Document, ScopeContext

DEFAULT_GLOBS = ["README.md", "docs/**/*.md", "adr/**/*.md", "runbooks/**/*.md"]


def repo_name(url: str) -> str:
    return url.rstrip("/").removesuffix(".git").split("/")[-1]


def _auth(url: str) -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    if token and url.startswith("https://github.com/"):
        return url.replace("https://", f"https://x-access-token:{token}@", 1)
    return url


class GitDocsSource(Source):
    name = "git"

    def _globs(self, ctx: ScopeContext) -> list[str]:
        return (ctx.config.get("globs") or self.option("globs")
                or [g.strip() for g in self.env("GIT_DOC_GLOBS").split(",") if g.strip()] or DEFAULT_GLOBS)

    def documents(self, ctx: ScopeContext, filter: dict | None = None):
        globs = self._globs(ctx)
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

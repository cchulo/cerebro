"""Local files adapter: ingest text files from directories mounted into the ingest container.
Useful for exports (a wiki dump, generated docs, a shared drive sync) and as the simplest custom source.

Scope config:  docs: { files: { paths: ["/data/docs/public"], globs: ["**/*.md", "**/*.txt"] } }
"""
import hashlib
from pathlib import Path
from .base import Source, Document, ScopeContext

DEFAULT_GLOBS = ["**/*.md", "**/*.markdown", "**/*.txt", "**/*.rst"]


class FilesSource(Source):
    name = "files"

    def documents(self, ctx: ScopeContext, filter: dict | None = None):
        for base in ctx.config.get("paths", []):
            root = Path(base)
            if not root.is_dir():
                continue
            for pattern in ctx.config.get("globs", DEFAULT_GLOBS):
                for f in sorted(root.glob(pattern)):
                    if not f.is_file():
                        continue
                    rel = f.relative_to(root).as_posix()
                    text = f.read_text(errors="ignore")
                    yield Document(key=f"{root.name}/{rel}", version=hashlib.sha256(text.encode()).hexdigest()[:16],
                                   text=f"Source: {root}/{rel}\n\n{text}", title=rel)

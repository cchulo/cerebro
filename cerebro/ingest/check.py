"""Develop and debug a source plugin without an index: list what it would yield for a scope.

    cerebro ingest check <scope> <source> [--limit N] [--filter '{"space": "ENG"}'] [--full]
"""
from __future__ import annotations
import json, time
from typing import Callable
from .runtime import Ingest


def check(ingest: Ingest, scope: str, source: str, *, filter: dict | str | None = None, limit: int = 20,
          full: bool = False, out: Callable[[str], None] = print) -> int:
    """Exit code: 0 listed, 1 unknown scope/source, 2 plugin not configured."""
    out("available sources: " + ", ".join(f"{n} ({o})" for n, o in sorted(ingest.sources.available().items())))
    if scope not in ingest.config.scopes:
        out(f"unknown scope {scope}; scopes: {ingest.scopes()}")
        return 1
    cfg = ingest.docs_config(scope).get(source)
    if cfg is None:
        out(f"scope {scope} does not list source {source} under docs:; listed: {list(ingest.docs_config(scope))}")
        return 1
    src = ingest.sources.load(source)
    out(f"{source}: configured={src.configured()} options={src.options} scope-config={cfg}")
    if not src.configured():
        return 2
    ctx = ingest.scope_context(scope, source)
    flt = json.loads(filter) if isinstance(filter, str) else filter
    t0, n, chars = time.monotonic(), 0, 0
    for doc in src.documents(ctx, flt or None):
        n += 1; chars += len(doc.text)
        if n <= limit:
            body = doc.text if full else doc.text[:160].replace("\n", " ⏎ ")
            out(f"- {doc.key}  v={doc.version}  title={doc.title!r}\n    {body}")
    out(f"\n{n} documents, {chars} chars, {time.monotonic() - t0:.1f}s" + ("" if n <= limit else f" (showing first {limit})"))
    return 0

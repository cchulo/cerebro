"""Develop and debug an adapter without LightRAG: list what it would yield for a scope.

  python -m ingest.check <scope> <source> [--limit N] [--filter '{"space": "ENG"}'] [--full]
  (inside the container:  make source-check SCOPE=public SOURCE=confluence)
"""
import argparse, json, sys, time
from . import scopes, sources
from .sources import ScopeContext


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scope"); ap.add_argument("source")
    ap.add_argument("--limit", type=int, default=20); ap.add_argument("--filter", default=None)
    ap.add_argument("--full", action="store_true", help="print full text instead of a preview")
    a = ap.parse_args()
    print("available sources:", ", ".join(f"{n} ({o})" for n, o in sorted(sources.available().items())))
    if a.scope not in scopes.SCOPES:
        sys.exit(f"unknown scope {a.scope}; scopes: {list(scopes.SCOPES)}")
    cfg = scopes.docs_config(a.scope).get(a.source)
    if cfg is None:
        sys.exit(f"scope {a.scope} does not list source {a.source} under docs:; listed: {list(scopes.docs_config(a.scope))}")
    src = sources.load(a.source, scopes.SOURCES.get(a.source))
    print(f"{a.source}: configured={src.configured()} options={src.options} scope-config={cfg}")
    if not src.configured():
        return 2
    ctx = ScopeContext(scope=a.scope, config=cfg, repos=scopes.code_repos(a.scope))
    flt = json.loads(a.filter) if a.filter else None
    t0, n, chars = time.monotonic(), 0, 0
    for doc in src.documents(ctx, flt):
        n += 1; chars += len(doc.text)
        if n <= a.limit:
            body = doc.text if a.full else doc.text[:160].replace("\n", " ⏎ ")
            print(f"- {doc.key}  v={doc.version}  title={doc.title!r}\n    {body}")
    print(f"\n{n} documents, {chars} chars, {time.monotonic() - t0:.1f}s" + ("" if n <= a.limit else f" (showing first {a.limit})"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

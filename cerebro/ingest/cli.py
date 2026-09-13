"""cerebro ingest: the ingest engine's commands.

    cerebro ingest serve  [--host H] [--port P] [--no-scheduler]       the FastAPI service (webhooks + schedule)
    cerebro ingest sync   [source] [--scope s ...] [--filter json]       one sync now, report as JSON
    cerebro ingest check  <scope> <source> [--filter json] [--limit n] [--full]   list what a plugin yields, no index

All take -c/--config (default $CEREBRO_CONFIG or cerebro.yaml).
"""
from __future__ import annotations
import argparse, json, logging, os, sys
from cerebro.core import load_config
from .runtime import Ingest


def _ingest(args) -> Ingest:
    return Ingest.from_config(load_config(args.config))


def _serve(args) -> int:
    import uvicorn
    from .service import create_app
    app = create_app(_ingest(args), scheduler=not args.no_scheduler)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def _sync(args) -> int:
    from .sync import sync
    flt = json.loads(args.filter) if args.filter else None
    report = sync(_ingest(args), args.source, flt, args.scope or None)
    print(json.dumps(report, indent=2, default=str))
    return 0


def _check(args) -> int:
    from .check import check
    return check(_ingest(args), args.scope, args.source, filter=args.filter, limit=args.limit, full=args.full)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=os.environ.get("CEREBRO_LOG_LEVEL", "INFO"))
    p = argparse.ArgumentParser(prog="cerebro ingest", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default=os.environ.get("CEREBRO_CONFIG", "cerebro.yaml"))
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the ingest service")
    s.add_argument("--host", default=os.environ.get("INGEST_HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(os.environ.get("INGEST_PORT", "8080")))
    s.add_argument("--no-scheduler", action="store_true", help="webhooks and /sync only, no cron")
    s.set_defaults(fn=_serve)
    y = sub.add_parser("sync", help="sync now")
    y.add_argument("source", nargs="?", default=None, help="one plugin (default: every plugin)")
    y.add_argument("--scope", action="append", help="limit to a scope (repeatable)")
    y.add_argument("--filter", default=None, help="plugin filter as JSON, as a webhook would send")
    y.set_defaults(fn=_sync)
    c = sub.add_parser("check", help="list what a plugin yields for a scope, without an index")
    c.add_argument("scope"); c.add_argument("source")
    c.add_argument("--filter", default=None); c.add_argument("--limit", type=int, default=20)
    c.add_argument("--full", action="store_true", help="print full text instead of a preview")
    c.set_defaults(fn=_check)
    args = p.parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())

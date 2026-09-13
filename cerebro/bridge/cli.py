"""cerebro bridge: run the stdio-to-HTTP bridge of one code unit (the entrypoint of images/code-unit).

    cerebro bridge serve --unit code-public --workspace /workspace --port 8045 [--engine tokensave]

Repositories come from CEREBRO_REPOS (JSON, set by the adapter's UnitSpec) and the unit name from --unit or
CEREBRO_UNIT. `--engine module:Class` swaps the engine (tests use a fake stdio server that way).
"""
from __future__ import annotations
import argparse, logging, os
from .engine import load_engine
from .server import Bridge, build_app
from .workspace import UNIT_ENV, REPOS_ENV, Workspace, entries_from_env


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cerebro bridge", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the bridge")
    s.add_argument("--unit", default=os.environ.get(UNIT_ENV), help=f"unit name (default: ${UNIT_ENV})")
    s.add_argument("--workspace", default=os.environ.get("CEREBRO_WORKSPACE", "/workspace"))
    s.add_argument("--host", default=os.environ.get("CEREBRO_BRIDGE_HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(os.environ.get("CEREBRO_BRIDGE_PORT", "8045")))
    s.add_argument("--engine", default=os.environ.get("CEREBRO_BRIDGE_ENGINE", "tokensave"), help="tokensave | module:Class")
    s.add_argument("--engine-bin", default=None, help="path to the engine binary (default: PATH)")
    s.add_argument("--repos", default=None, help=f"JSON list of repos (default: ${REPOS_ENV})")
    s.add_argument("--log-level", default=os.environ.get("CEREBRO_LOG_LEVEL", "info"))
    return p


def make_bridge(args) -> Bridge:
    if not args.unit:
        raise SystemExit(f"--unit or ${UNIT_ENV} is required")
    ws = Workspace(args.workspace, entries_from_env(args.repos))
    engine = load_engine(args.engine, ws, binary=args.engine_bin)
    return Bridge(args.unit, ws, engine)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(name)s %(levelname)s %(message)s")
    import uvicorn
    bridge = make_bridge(args)
    uvicorn.run(build_app(bridge), host=args.host, port=args.port, log_level=args.log_level, lifespan="on")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

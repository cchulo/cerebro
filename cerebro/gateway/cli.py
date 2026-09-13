"""cerebro gateway: the command line of the MCP gateway.

    cerebro gateway serve [-c cerebro.yaml] [--log-level info]

Loads the config, builds every adapter once (Gateway.from_config) and serves the Starlette app with uvicorn on
gateway.host:gateway.port, listening on $CEREBRO_GATEWAY_BIND when the provisioner sets it (a workload must
listen on 0.0.0.0 for its published port); identity.mode none forces 127.0.0.1 unless identity.allow_remote.
"""
from __future__ import annotations
import argparse
import logging
import sys


def _serve(args) -> int:
    import uvicorn
    from cerebro.core import load_config
    from .server import Gateway

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(levelname)s: %(message)s")
    config = load_config(args.config)
    gw = Gateway.from_config(config)
    log = logging.getLogger("cerebro.gateway")
    log.info("identity.mode=%s (%s), policy=%s, engines docs=%s code=%s memory=%s", config.identity.mode, gw.identity.name,
             gw.policy.name, getattr(gw.docs, "name", None), getattr(gw.code, "name", None), getattr(gw.memory, "name", None))
    for note in gw.notes:
        log.warning("%s", note)
    bind = gw.bind_host()
    log.info("serving MCP at http://%s:%s%s (listening on %s, resource %s)", gw.host, config.gateway.port,
             config.gateway.path, bind, config.resource_id())
    uvicorn.run(gw.app, host=bind, port=config.gateway.port, log_level=args.log_level.lower())
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(prog="cerebro gateway", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the MCP gateway")
    s.add_argument("-c", "--config", default=None, help="cerebro.yaml (default: $CEREBRO_CONFIG or ./cerebro.yaml)")
    s.add_argument("--log-level", default="info")
    s.set_defaults(fn=_serve)
    args = p.parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())

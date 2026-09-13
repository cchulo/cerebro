"""cerebro: the command line.

    cerebro validate [-c cerebro.yaml]      check the config, print the resolved units
    cerebro schema                          JSON schema of cerebro.yaml (editor completion)
    cerebro <component> ...                 gateway | ingest | provision | index | bridge | smoke (each registers its own)
"""
from __future__ import annotations
import argparse, importlib, json, sys

COMPONENTS = {
    "gateway": "cerebro.gateway.cli",
    "ingest": "cerebro.ingest.cli",
    "provision": "cerebro.provision_cli",
    "index": "cerebro.adapters.code.index_cli",
    "bridge": "cerebro.bridge.cli",
    "smoke": "cerebro.smoke",
}


def _validate(args) -> int:
    from cerebro.core import load_config, code_units, docs_unit_name
    cfg = load_config(args.config)
    print(f"ok: {args.config} (identity.mode={cfg.identity.mode}, target={cfg.provisioning.target})")
    for s in cfg.scopes:
        print(f"  scope {s}: groups={cfg.scopes[s].groups} docs={sorted(cfg.scopes[s].docs)} -> {docs_unit_name(s)}")
    for u in code_units(cfg):
        print(f"  code unit {u.name} ({u.kind}, scope {u.scope}): " + ", ".join(
            f"{r.name}[{','.join(r.branches) or 'default'}]" for r in u.repos))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in COMPONENTS:
        mod = importlib.import_module(COMPONENTS[argv[0]])
        return int(mod.main(argv[1:]) or 0)
    p = argparse.ArgumentParser(prog="cerebro", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate"); v.add_argument("-c", "--config", default="cerebro.yaml"); v.set_defaults(fn=_validate)
    s = sub.add_parser("schema"); s.set_defaults(fn=lambda a: (print(json.dumps(__import__("cerebro.core", fromlist=["json_schema"]).json_schema(), indent=2)) or 0))
    for name in COMPONENTS:
        sub.add_parser(name, help=f"{name} commands (cerebro {name} --help)", add_help=False)
    args = p.parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())

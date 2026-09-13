"""cerebro provision: render and drive the stack from cerebro.yaml.

    cerebro provision render   [-c cerebro.yaml] [--target compose|kubernetes] [-o deploy/generated]
    cerebro provision up       [unit ...]    render, then start every unit (or only the listed ones)
    cerebro provision down     [--volumes]   stop and remove; --volumes also deletes the data
    cerebro provision status   [unit ...]
    cerebro provision job      <name> [--wait]
    cerebro provision plan                   print the units and jobs without rendering
    cerebro provision operator               the idle-TTL operator (kubernetes target)

Every command takes -c/--config, --target (default: provisioning.target), --env-file (default secrets.env) and
-o/--output (default deploy/generated). Secrets come from the env file at run time; nothing rendered contains them.
"""
from __future__ import annotations
import argparse, asyncio, logging, os, pathlib, shutil, subprocess, sys
from .core import AdapterContext, load_config, registry
from .core.contracts.provision import JobSpec, UnitRef, UnitSpec
from .provision.plan import plan

log = logging.getLogger("cerebro.provision.cli")


def build(args):
    cfg = load_config(args.config)
    ctx = AdapterContext(cfg)
    target = args.target or cfg.provisioning.target
    opts = {**cfg.provisioning.options, "output_dir": args.output, "env_file": args.env_file, "config_path": args.config}
    adapter = registry.build("provision", target, opts, ctx)
    ctx.locator = adapter
    return cfg, ctx, adapter


def write_files(files: dict[str, str]) -> list[pathlib.Path]:
    out = []
    for path, content in files.items():
        p = pathlib.Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        out.append(p)
    return out


def render(cfg, ctx, adapter, args) -> tuple[list[UnitSpec], list[JobSpec]]:
    units, jobs = plan(cfg, ctx)
    paths = write_files(adapter.render(units, jobs))
    print(f"rendered {len(paths)} file(s) under {args.output} for target {adapter.name}: "
          f"{len(units)} units, {len(jobs)} jobs")
    if adapter.name == "kubernetes":
        env = pathlib.Path(args.env_file)
        dest = adapter.output_dir / env.name
        if env.is_file():
            shutil.copy(env, dest); dest.chmod(0o600)
            print(f"copied {env} -> {dest} (Secret cerebro-secrets is built from it at apply time; gitignored)")
        else:
            print(f"note: {env} not found; create it before `kubectl apply -k {adapter.output_dir}` "
                  f"(one KEY=value line per name in secrets.keys)")
    return units, jobs


def _selected(units: list[UnitSpec], names: list[str]) -> list[UnitSpec]:
    if not names:
        return units
    known = {u.name: u for u in units}
    missing = [n for n in names if n not in known]
    if missing:
        raise SystemExit(f"unknown unit(s) {missing}; known: {sorted(known)}")
    return [known[n] for n in names]


def cmd_render(args) -> int:
    render(*build(args), args)
    return 0


def cmd_plan(args) -> int:
    cfg, ctx, _ = build(args)
    units, jobs = plan(cfg, ctx)
    for u in units:
        print(f"unit {u.name:<28} role={u.role:<9} image={u.image} port={u.http_port}"
              + (f" scope={u.scope}" if u.scope else "") + (f" idle_ttl={u.idle_ttl}" if u.idle_ttl else "")
              + (f" secrets={u.secret_env}" if u.secret_env else ""))
    for j in jobs:
        print(f"job  {j.name:<28} schedule={j.schedule or 'on demand':<12} image={j.image}")
    return 0


async def _up(cfg, ctx, adapter, args) -> int:
    units, _ = render(cfg, ctx, adapter, args)
    if adapter.name == "kubernetes":
        subprocess.run(["kubectl", "apply", "-k", str(adapter.output_dir)], check=True)
    failed = 0
    for u in _selected(units, args.units):
        ep = await adapter.ensure(u)
        print(f"{u.name:<28} {'ready' if ep.ready else 'NOT READY'}  {ep.url}")
        failed += not ep.ready
    return 1 if failed else 0


def cmd_up(args) -> int:
    return asyncio.run(_up(*build(args), args))


def cmd_down(args) -> int:
    _, _, adapter = build(args)
    asyncio.run(adapter.down(volumes=args.volumes))
    print(f"{adapter.name}: down" + (" (volumes deleted)" if args.volumes else ""))
    return 0


async def _status(cfg, ctx, adapter, args) -> int:
    units, _ = plan(cfg, ctx)
    for u in _selected(units, args.units):
        st = await adapter.status(UnitRef(name=u.name))
        state = "ready" if st.ready else ("stopped" if st.exists and not st.replicas else ("starting" if st.exists else "absent"))
        used = f"  last used {st.last_used:%Y-%m-%d %H:%M}Z" if st.last_used else ""
        print(f"{u.name:<28} {state:<9} {st.message or ''}{used}")
    return 0


def cmd_status(args) -> int:
    return asyncio.run(_status(*build(args), args))


def cmd_job(args) -> int:
    cfg, ctx, adapter = build(args)
    _, jobs = plan(cfg, ctx)
    job = next((j for j in jobs if j.name == args.name), None)
    if job is None:
        raise SystemExit(f"unknown job '{args.name}'; known: {[j.name for j in jobs]}")
    run_id = asyncio.run(adapter.run_job(job, wait=args.wait))
    print(f"{job.name}: {'finished' if args.wait else 'started'} ({run_id})")
    return 0


def cmd_operator(args) -> int:
    from .provision import operator
    cfg = load_config(args.config)
    return operator.run(cfg.provisioning.namespace)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(prog="cerebro provision", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=os.environ.get("CEREBRO_CONFIG", "cerebro.yaml"))
    common.add_argument("--target", choices=["compose", "kubernetes"], default=None)
    common.add_argument("--env-file", default="secrets.env")
    common.add_argument("-o", "--output", default="deploy/generated")
    common.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    add = lambda name, **kw: sub.add_parser(name, parents=[common], **kw)  # noqa: E731
    add("render", help="write the manifests for the target").set_defaults(fn=cmd_render)
    add("plan", help="print units and jobs").set_defaults(fn=cmd_plan)
    up = add("up", help="render + start units"); up.add_argument("units", nargs="*"); up.set_defaults(fn=cmd_up)
    down = add("down", help="stop and remove"); down.add_argument("--volumes", action="store_true"); down.set_defaults(fn=cmd_down)
    st = add("status"); st.add_argument("units", nargs="*"); st.set_defaults(fn=cmd_status)
    job = add("job", help="run a job now"); job.add_argument("name"); job.add_argument("--wait", action="store_true"); job.set_defaults(fn=cmd_job)
    add("operator", help="idle-TTL operator (kubernetes)").set_defaults(fn=cmd_operator)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())

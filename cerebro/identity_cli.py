"""cerebro identity: the authorization server of identity.mode builtin, driven from the operator's machine.

    cerebro identity seed [-c cerebro.yaml] [--env-file secrets.env] [--admin-url URL] [--timeout 300]

Builds the `auth` adapter (identity.server.type), waits for ready(), then seed(users, groups, resource_id): the
users are identity.users, the groups every scope's `groups` minus policy.always_groups (`everyone` is not a group
anyone is put in), the resource id the gateway's (Config.resource_id()). Idempotent: run it again after editing
users or scopes. Prints the adapter's report as JSON and the first-login instructions.

The admin API is reached at --admin-url, default the origin of the adapter's issuer() (identity.server.public_url,
which compose publishes on the host's loopback); the admin credentials are the secrets identity.server names
(CEREBRO_AUTH_ADMIN_USER / CEREBRO_AUTH_ADMIN_PASSWORD), read from the process environment or --env-file, the way
the units read them. `cerebro provision up` reminds you to run this in builtin mode.
"""
from __future__ import annotations
import argparse, asyncio, json, logging, os, sys, time
from urllib.parse import urlsplit
from .core import AdapterContext, EnvSecrets, load_config, registry
from .core.config import Config, read_env_file
from .core.contracts.identity import AuthorizationServer

log = logging.getLogger("cerebro.identity")
READY_INTERVAL = 3.0          # seconds between ready() polls


def seed_groups(cfg: Config) -> list[str]:
    """Every group a scope grants, minus policy.always_groups, sorted; identity.users[].groups are added by seed()."""
    always = set(cfg.policy.always_groups)
    return sorted({g for s in cfg.scopes.values() for g in s.groups} - always)


def build_auth(cfg: Config, env_file: str | None, admin_url: str | None = None) -> AuthorizationServer:
    server = cfg.identity.server
    if cfg.identity.mode != "builtin" or server is None:
        raise SystemExit(f"identity.mode is {cfg.identity.mode}: `cerebro identity seed` is for mode builtin (identity.server)")
    ctx = AdapterContext(cfg, secrets=EnvSecrets(fallback=read_env_file(env_file)))
    auth = registry.build("auth", server.type, dict(server.options), ctx)
    url = admin_url or auth.options.get("admin_url")
    if not url:                                    # from this machine the unit is reached where browsers reach it
        u = urlsplit(auth.issuer())
        url = f"{u.scheme}://{u.netloc}"
    auth.options["admin_url"] = url
    return auth


async def wait_ready(auth: AuthorizationServer, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if await auth.ready():
            return True
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(READY_INTERVAL)


def first_login_text(cfg: Config, auth: AuthorizationServer, report: dict) -> str:
    server = cfg.identity.server
    public = auth.issuer()
    created = report.get("created", {}).get("users", [])
    action = report.get("first_login_action", "")
    lines = [f"issuer {report.get('issuer', public)}; the gateway validates tokens for {report.get('resource', cfg.resource_id())}"]
    if created:
        pw = "the temporary password printed above" if report.get("password_generated") else "the value of CEREBRO_SEED_PASSWORD"
        step = "register a passkey" if action.startswith("webauthn") else "set a new password"
        lines.append(f"first login for {', '.join(created)}: open {public}/account, sign in with {pw}, then {step}")
    lines.append(f"MCP clients discover {report.get('issuer', public)} from the gateway's metadata and log in with PKCE "
                 f"(client {report.get('client_id', 'cerebro-mcp')} or dynamic registration); admin console: "
                 f"{auth.options['admin_url']}/admin (realm {server.realm})")
    return "\n".join(lines)


def cmd_seed(args) -> int:
    cfg = load_config(args.config, env={**read_env_file(args.env_file), **os.environ})
    auth = build_auth(cfg, args.env_file, args.admin_url)
    users, groups = cfg.identity.users, seed_groups(cfg)
    print(f"seeding {auth.name} at {auth.options['admin_url']}: {len(users)} user(s), groups {groups}, resource {cfg.resource_id()}",
          file=sys.stderr)
    if not asyncio.run(wait_ready(auth, args.timeout)):
        raise SystemExit(f"{auth.name} at {auth.options['admin_url']} is not ready after {args.timeout:.0f}s "
                         f"(is the stack up? `cerebro provision status auth`)")
    report = asyncio.run(auth.seed(users, groups, cfg.resource_id()))
    print(json.dumps(report, indent=2, default=str))
    print(first_login_text(cfg, auth, report), file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(prog="cerebro identity", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=os.environ.get("CEREBRO_CONFIG", "cerebro.yaml"))
    common.add_argument("--env-file", default="secrets.env", help="KEY=value file: ${NAME} in cerebro.yaml and the admin secrets")
    common.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed", parents=[common], help="create realm, groups, users and the gateway's audience (idempotent)")
    s.add_argument("--admin-url", default=None, help="where this machine reaches the admin API (default: the issuer's origin)")
    s.add_argument("--timeout", type=float, default=300, help="seconds to wait for the server to be ready")
    s.set_defaults(fn=cmd_seed)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())

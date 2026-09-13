"""plan(): everything the provisioner has to run for one cerebro.yaml.

    units, jobs = plan(config, ctx)

Base infrastructure (postgres, ollama when the inference endpoints point at it, gateway, ingest) plus what every
adapter contributes through `units()` / `jobs()`: the engine adapters from `engines.{docs,code,memory}`, the
authorization server in `identity.mode: builtin`, and one `mcp-<plugin>` unit per source plugin that declares an
`McpUpstream` for its live part.

Conventions every adapter can rely on (renderers implement them, tests pin them):

    postgres      host `postgres`, port 5432, user `cerebro`, password in secret POSTGRES_PASSWORD,
                  databases lightrag / hindsight / keycloak / cerebro (pgvector in lightrag and cerebro)
    ollama        host `ollama`, port 11434 (only when inference.llm/embed base_url points at it)
    ${NAME}       in any env value means "the secret NAME": the planner moves NAME into secret_env and the renderer
                  makes the runtime substitute it (compose: `${NAME}` from --env-file; kubernetes: `$(NAME)` from a
                  secretKeyRef). `${NAME:-default}` becomes the literal default unless NAME is in secrets.keys.
    volumes       a volume named V on unit U is the physical volume `U-V`; a job that lists U in
                  VolumeSpec.shared_with (and V under the same name) mounts that same volume.
    ports         ports[0] is the HTTP port other units talk to; Locator.endpoint(unit) returns http://<unit>:<port>.
    files         UnitSpec.files / JobSpec.files: mount path -> content; small non-secret files (init scripts).
"""
from __future__ import annotations
import logging, pathlib, re
from typing import Any
from urllib.parse import urlparse
from ..core import registry
from ..core.config import Config
from ..core.context import AdapterContext
from ..core.contracts.provision import JobSpec, PortSpec, UnitSpec, VolumeSpec
from ..core.contracts.sources import discover
from .common import label_value

log = logging.getLogger("cerebro.provision")

CONFIG_MOUNT = "/config/cerebro.yaml"     # where gateway and ingest read cerebro.yaml
PLUGINS_MOUNT = "/plugins"                # where gateway and ingest find plugins/*.py
POSTGRES_USER = "cerebro"
POSTGRES_IMAGE = "pgvector/pgvector:pg16"
OLLAMA_IMAGE = "ollama/ollama:0.33.3"
GATEWAY_IMAGE, GATEWAY_BUILD = "cerebro/gateway", "images/gateway"
INGEST_IMAGE, INGEST_BUILD = "cerebro/ingest", "images/ingest"
ENGINE_ROLES = ("docs", "code", "memory")
_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------------------------- secrets in env
def resolve_env_refs(env: dict[str, str], secret_keys: set[str] | list[str]) -> tuple[dict[str, str], list[str]]:
    """Normalise `${NAME}` / `${NAME:-default}` in env values. Returns (env, secret names referenced).

    Every bare `${NAME}` is a secret reference and stays `${NAME}` (the renderer turns it into the runtime's own
    syntax). `${NAME:-default}` is a secret reference only when NAME is declared in secrets.keys; otherwise it is
    replaced by the literal default so the two targets never disagree about what a unit sees.
    """
    keys, names, out = set(secret_keys), [], {}
    for k, v in env.items():
        def sub(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            if default is not None and name not in keys:
                return default
            if name not in names:
                names.append(name)
            return "${" + name + "}"
        out[k] = _REF.sub(sub, v) if isinstance(v, str) else v
    return out, names


def _normalise(spec: UnitSpec | JobSpec, secret_keys: list[str]) -> None:
    env, refs = resolve_env_refs(spec.env, secret_keys)
    spec.env = env
    for n in refs:
        if n not in secret_keys:
            log.warning("%s references secret %s which is not declared in secrets.keys", spec.name, n)
        if n not in spec.secret_env:
            spec.secret_env.append(n)


# ---------------------------------------------------------------------------------------------- base units
def init_sql() -> str:
    p = _ROOT / "deploy" / "postgres" / "init.sql"
    return p.read_text() if p.exists() else "-- deploy/postgres/init.sql missing at render time\n"


def base_units(config: Config, engine_units: list[UnitSpec]) -> list[UnitSpec]:
    labels = {"cerebro.io/project": config.provisioning.project}
    units = [UnitSpec(
        name="postgres", role="infra", image=POSTGRES_IMAGE, stateful=True,
        ports=[PortSpec(name="postgres", port=5432)],
        env={"POSTGRES_USER": POSTGRES_USER, "POSTGRES_DB": "postgres", "PGDATA": "/var/lib/postgresql/data/pgdata"},
        secret_env=["POSTGRES_PASSWORD"],
        volumes=[VolumeSpec(name="data", mount_path="/var/lib/postgresql/data", size="20Gi")],
        files={"/docker-entrypoint-initdb.d/init.sql": init_sql()},
        health_path=None, health_cmd=["pg_isready", "-U", POSTGRES_USER, "-d", "postgres"], labels=dict(labels))]
    if needs_ollama(config):
        units.append(UnitSpec(
            name="ollama", role="inference", image=OLLAMA_IMAGE, ports=[PortSpec(port=11434)],
            env={"OLLAMA_KEEP_ALIVE": "30m", "OLLAMA_NUM_PARALLEL": "4"},
            volumes=[VolumeSpec(name="models", mount_path="/root/.ollama", size="60Gi")],
            health_path=None, health_cmd=["ollama", "list"], labels=dict(labels)))
    common_env = {"CEREBRO_CONFIG": CONFIG_MOUNT, "CEREBRO_PLUGINS_DIR": PLUGINS_MOUNT}
    engines = [u.name for u in engine_units]
    units.append(UnitSpec(
        name="gateway", role="gateway", image=GATEWAY_IMAGE, build=GATEWAY_BUILD,
        ports=[PortSpec(port=config.gateway.port)], env={**common_env, "PORT": str(config.gateway.port)},
        secret_env=list(config.secrets.keys), depends_on=["postgres", *engines], labels=dict(labels)))
    units.append(UnitSpec(
        name="ingest", role="ingest", image=INGEST_IMAGE, build=INGEST_BUILD, ports=[PortSpec(port=8080)],
        env=dict(common_env), secret_env=list(config.secrets.keys),
        volumes=[VolumeSpec(name="state", mount_path="/state", size="1Gi")],
        depends_on=["postgres", *[u.name for u in engine_units if u.role == "docs"]], labels=dict(labels)))
    return units


def needs_ollama(config: Config) -> bool:
    return any(urlparse(e.base_url).hostname == "ollama" for e in (config.inference.llm, config.inference.embed))


# ---------------------------------------------------------------------------------------------- adapters
def _build(kind: str, type_: str, options: dict[str, Any], ctx: AdapterContext):
    try:
        return registry.build(kind, type_, options, ctx)
    except LookupError as e:
        log.info("plan: skipping %s adapter '%s' (%s)", kind, type_, e)
        return None


def adapter_instances(config: Config, ctx: AdapterContext) -> list:
    """The adapters whose units the plan includes: engines, and the auth server in builtin mode."""
    out = []
    for kind in ENGINE_ROLES:
        cfg = getattr(config.engines, kind)
        if cfg is None:
            continue
        a = _build(kind, cfg.type, cfg.options, ctx)     # unit / idle_ttl / resources: adapters read config.engines.<kind>
        if a is not None:
            out.append(a)
    if config.identity.mode == "builtin" and config.identity.server:
        a = _build("auth", config.identity.server.type, config.identity.server.options, ctx)
        if a is not None:
            out.append(a)
    return out


def mcp_units(config: Config, plugins_dir: str | pathlib.Path | None = None) -> list[UnitSpec]:
    """One `mcp-<plugin>` unit per plugin with an McpUpstream and a live part, unless cerebro.yaml turns it off:

        sources:
          confluence: { live: { enabled: false } }        # no live fallback at all
          confluence: { live: { via: rest } }             # live part talks REST itself, no upstream
          confluence: { live: { url: http://... } }       # an upstream that already runs elsewhere
    """
    out = []
    d = pathlib.Path(plugins_dir or config.gateway.plugins_dir)
    if not d.is_dir():
        return out
    for name, plugin in discover(d).items():
        if not (plugin.mcp and plugin.live):
            continue
        live = (config.sources.get(name) or {}).get("live") or {}
        if live.get("enabled", True) is False or live.get("via", "mcp") == "rest" or live.get("url"):
            continue
        m = plugin.mcp
        out.append(UnitSpec(name=f"mcp-{name}", role="mcp", image=m.image, args=list(m.args), env=dict(m.env),
                            ports=[PortSpec(port=m.port)], health_path=None, labels={"cerebro.io/plugin": name}))
    return out


# ---------------------------------------------------------------------------------------------- plan
def plan(config: Config, ctx: AdapterContext | None = None, *, plugins_dir: str | pathlib.Path | None = None,
         adapters: list | None = None) -> tuple[list[UnitSpec], list[JobSpec]]:
    """The complete plan. `adapters` overrides registry lookup (tests pass fakes)."""
    ctx = ctx or AdapterContext(config)
    adapters = adapter_instances(config, ctx) if adapters is None else list(adapters)
    engine_units: list[UnitSpec] = []
    jobs: list[JobSpec] = []
    for a in adapters:
        for u in a.units():
            u.labels.setdefault("cerebro.io/adapter", label_value(f"{a.kind}.{a.name}"))
            engine_units.append(u)
        jobs.extend(a.jobs())
    engine_units.extend(mcp_units(config, plugins_dir))
    units = base_units(config, engine_units) + engine_units
    seen: set[str] = set()
    for spec in [*units, *jobs]:
        if spec.name in seen:
            raise ValueError(f"plan: duplicate unit/job name '{spec.name}'")
        seen.add(spec.name)
        spec.labels.setdefault("cerebro.io/project", config.provisioning.project)
        _normalise(spec, config.secrets.keys)
    return units, jobs

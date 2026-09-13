"""provisioning.target: compose -- Docker Compose, the dev / single-box path.

    render()   -> deploy/generated/compose.yaml: one service per unit, one (profile `jobs`) service per job, and a
                  `scheduler` service when any job has a cron schedule
    ensure()   -> docker compose -p <project> -f <rendered> --env-file secrets.env up -d <unit>
    release()  -> ... stop <unit>          status() -> ... ps --format json <unit>
    run_job()  -> ... --profile jobs run --rm <job>
    endpoint() -> http://<unit>:<port>     (compose DNS: the service name is the hostname)

Secrets: a unit's `secret_env` names become `NAME: ${NAME}` in the service environment and `${NAME}` references inside
other env values are left as they are; `docker compose --env-file secrets.env` substitutes them at run time, so the
rendered file never holds a value. Run `docker compose` with that env file (the CLI does) or the variables are empty.

Builds: `UnitSpec.build` names the directory holding the Dockerfile (images/gateway); the context is the repository
root, because the Dockerfiles COPY pyproject.toml / cerebro / plugins from there.

Ports: only the gateway is published (gateway.host:gateway.port, 127.0.0.1 by default). Engines, Postgres, Ollama
and MCP upstreams are reachable on the compose network only.

Health checks: `health_cmd` becomes a CMD check; `health_path` becomes a CMD-SHELL check that tries curl, then wget,
then python3 (engine images differ in what they ship). An image with none of the three never becomes healthy: set
health_path=None on such a unit and dependants fall back to `service_started`.

Scheduled jobs: compose has no cron. The `scheduler` service is `docker:<v>-cli` running busybox crond with one
crontab line per scheduled job that executes `docker compose ... --profile jobs run --rm <job>` through the host's
Docker socket (mounted read-write) with the repository mounted read-only at /workspace. Dev only; on Kubernetes the
same jobs are CronJobs.

Options (provisioning.options, or set by the CLI): output_dir (deploy/generated), env_file (secrets.env),
config_path (cerebro.yaml), ready_timeout (300 s), docker_cli_image.
"""
from __future__ import annotations
import asyncio, json, logging, os, pathlib, re, shlex, time
from datetime import datetime, timezone
import yaml
from ...core.contracts.provision import Endpoint, JobSpec, Provisioner, UnitRef, UnitSpec, UnitStatus
from ...provision.common import PORT_LABEL, labels_for, volume_key
from ...provision.plan import CONFIG_MOUNT, PLUGINS_MOUNT

log = logging.getLogger("cerebro.provision.compose")
_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
DEFAULT_PORT = 8080
SCHEDULER = "scheduler"


def compose_value(value: str) -> str:
    """Escape `$` for compose interpolation, except `${NAME}` secret references which compose must substitute."""
    parts, out, last = _REF.finditer(value), [], 0
    for m in parts:
        out.append(value[last:m.start()].replace("$", "$$"))
        out.append(m.group(0))
        last = m.end()
    out.append(value[last:].replace("$", "$$"))
    return "".join(out)


class Adapter(Provisioner):
    name = "compose"

    def __init__(self, options=None, ctx=None):
        super().__init__(options, ctx)
        cfg = ctx.config if ctx else None
        self.project: str = self.option("project") or (cfg.provisioning.project if cfg else "cerebro")
        self.output_dir = pathlib.Path(self.option("output_dir", "deploy/generated"))
        self.compose_file = self.output_dir / "compose.yaml"
        self.env_file: str = self.option("env_file", "secrets.env")
        self.config_path: str = self.option("config_path") or os.environ.get("CEREBRO_CONFIG", "cerebro.yaml")
        self.plugins_dir: str = cfg.gateway.plugins_dir if cfg else "plugins"
        self.gateway_bind = (cfg.gateway.host, cfg.gateway.port) if cfg else ("127.0.0.1", 8090)
        self.image_registry: str | None = cfg.provisioning.image_registry if cfg else None
        self.ports: dict[str, int] = {}
        self.profiles: dict[str, list[str]] = {}
        self.last_used: dict[str, datetime] = {}
        self._ports_loaded = False

    # ------------------------------------------------------------------------------------------ Locator
    def endpoint(self, unit: str) -> str:
        if unit not in self.ports and not self._ports_loaded:
            self._load_ports()
        return f"http://{unit}:{self.ports.get(unit, DEFAULT_PORT)}"

    def _load_ports(self) -> None:
        """Ports from the rendered file's cerebro.io/port labels, so endpoint() works without ensure() first."""
        self._ports_loaded = True
        try:
            doc = yaml.safe_load(self.compose_file.read_text()) or {}
        except (OSError, yaml.YAMLError):
            return
        for name, svc in (doc.get("services") or {}).items():
            port = (svc.get("labels") or {}).get(PORT_LABEL)
            if port:
                self.ports.setdefault(name, int(port))

    # ------------------------------------------------------------------------------------------ render
    def render(self, units: list[UnitSpec], jobs: list[JobSpec]) -> dict[str, str]:
        unit_names = {u.name for u in units}
        healthy = {u.name for u in units if u.health_cmd or u.health_path}
        services: dict[str, dict] = {}
        volumes: dict[str, dict] = {}
        configs: dict[str, dict] = {}
        for u in units:
            services[u.name] = self._service(u, unit_names, healthy, volumes, configs)
            self.ports[u.name] = u.http_port
        for j in jobs:
            services[j.name] = self._service(j, unit_names, healthy, volumes, configs)
        scheduled = [j for j in jobs if j.schedule]
        if scheduled:
            services[SCHEDULER] = self._scheduler(scheduled, configs)
        project_labels = {"cerebro.io/project": self.project}
        doc = {"name": self.project, "services": services}
        if volumes:
            doc["volumes"] = volumes
        if configs:
            doc["configs"] = configs
        doc["networks"] = {"default": {"labels": project_labels}}
        header = ("# GENERATED by `cerebro provision render` (target compose) from cerebro.yaml -- do not edit.\n"
                  f"# Run with: docker compose -p {self.project} -f {self.compose_file} --env-file {self.env_file} ...\n"
                  "# Secrets are ${NAME} references substituted from the env file; no value is stored here.\n")
        return {str(self.compose_file): header + yaml.safe_dump(doc, sort_keys=False, width=4096)}

    def _rel(self, path: str | os.PathLike) -> str:
        """Bind-mount and build paths are relative to the compose file's directory."""
        return os.path.relpath(os.path.abspath(path), os.path.abspath(self.output_dir))

    def _image(self, spec: UnitSpec | JobSpec) -> str:
        return f"{self.image_registry.rstrip('/')}/{spec.image}" if (spec.build and self.image_registry) else spec.image

    def _service(self, spec: UnitSpec | JobSpec, unit_names: set[str], healthy: set[str],
                 volumes: dict[str, dict], configs: dict[str, dict]) -> dict:
        is_unit = isinstance(spec, UnitSpec)
        labels = labels_for(spec, self.project)
        svc: dict = {"image": self._image(spec)}
        if spec.build:                                # images/<x>/Dockerfile builds from the repository root
            svc["build"] = {"context": self._rel("."), "dockerfile": f"{spec.build.rstrip('/')}/Dockerfile"}
        svc["restart"] = "unless-stopped" if is_unit else "no"
        if spec.command:
            svc["entrypoint"] = list(spec.command)
        if spec.args:
            svc["command"] = list(spec.args)
        env = {n: "${" + n + "}" for n in spec.secret_env}
        env.update({k: compose_value(str(v)) for k, v in spec.env.items()})
        if env:
            svc["environment"] = env
        if is_unit and spec.role == "gateway":
            host, port = self.gateway_bind
            svc["ports"] = [f"{host}:{port}:{spec.http_port}"]
        mounts = []
        for v in spec.volumes:
            key = volume_key(spec, v, unit_names)
            volumes.setdefault(key, {"labels": {"cerebro.io/project": self.project, **({"cerebro.io/scope": spec.scope} if spec.scope else {})}})
            mounts.append(f"{key}:{v.mount_path}" + (":ro" if v.read_only else ""))
        if is_unit and spec.role in ("gateway", "ingest"):
            mounts.append(f"{self._rel(self.config_path)}:{CONFIG_MOUNT}:ro")
            mounts.append(f"{self._rel(self.plugins_dir)}:{PLUGINS_MOUNT}:ro")
        if mounts:
            svc["volumes"] = mounts
        if spec.files:
            svc["configs"] = []
            for i, (path, content) in enumerate(spec.files.items()):
                cname = f"{spec.name}-file-{i}"
                configs[cname] = {"content": compose_value(content)}
                svc["configs"].append({"source": cname, "target": path})
        if is_unit:
            hc = self._healthcheck(spec)
            if hc:
                svc["healthcheck"] = hc
            if spec.labels.get("cerebro.io/profile"):
                svc["profiles"] = self.profiles[spec.name] = [spec.labels["cerebro.io/profile"]]
        else:
            svc["profiles"] = ["jobs"]
        deps = {d: {"condition": "service_healthy" if d in healthy else "service_started"}
                for d in spec.depends_on if d in unit_names}
        if deps:
            svc["depends_on"] = deps
        svc["labels"] = labels
        return svc

    @staticmethod
    def _healthcheck(spec: UnitSpec) -> dict | None:
        if spec.health_cmd:
            test = ["CMD", *spec.health_cmd]
        elif spec.health_path:
            url = f"http://localhost:{spec.http_port}{spec.health_path}"
            test = ["CMD-SHELL", f"curl -fsS {url} || wget -qO- {url} || "
                                 f"python3 -c \"import urllib.request; urllib.request.urlopen('{url}')\""]
        else:
            return None
        return {"test": test, "interval": "15s", "timeout": "5s", "retries": 20, "start_period": "30s"}

    def _scheduler(self, jobs: list[JobSpec], configs: dict[str, dict]) -> dict:
        base = (f"cd /workspace && docker compose -p {shlex.quote(self.project)} -f {shlex.quote(str(self.compose_file))} "
                f"--env-file {shlex.quote(self.env_file)} --profile jobs run --rm")
        lines = [f"{j.schedule} {base} {j.name} >> /proc/1/fd/1 2>&1" for j in jobs]
        configs["scheduler-crontab"] = {"content": compose_value("\n".join(lines) + "\n")}
        return {"image": self.option("docker_cli_image", "docker:27-cli"), "restart": "unless-stopped",
                "command": ["crond", "-f", "-l", "6"], "working_dir": "/workspace",
                "volumes": ["/var/run/docker.sock:/var/run/docker.sock", f"{self._rel('.')}:/workspace:ro"],
                "configs": [{"source": "scheduler-crontab", "target": "/etc/crontabs/root"}],
                "labels": {"cerebro.io/project": self.project, "cerebro.io/role": "scheduler"}}

    # ------------------------------------------------------------------------------------------ runtime
    def command(self, *args: str, profiles: list[str] | tuple[str, ...] = ()) -> list[str]:
        cmd = ["docker", "compose", "-p", self.project, "-f", str(self.compose_file)]
        if pathlib.Path(self.env_file).exists():
            cmd += ["--env-file", self.env_file]
        for p in profiles:
            cmd += ["--profile", p]
        return cmd + list(args)

    async def _run(self, *args: str, profiles=(), check: bool = True, capture: bool = True) -> str:
        cmd = self.command(*args, profiles=profiles)
        log.debug("$ %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE if capture else None, stderr=asyncio.subprocess.PIPE if capture else None)
        out, err = await proc.communicate()
        if check and proc.returncode:
            raise RuntimeError(f"{' '.join(cmd)} failed ({proc.returncode}): {(err or b'').decode().strip()}")
        return (out or b"").decode()

    async def ensure(self, spec: UnitSpec) -> Endpoint:
        self.ports[spec.name] = spec.http_port
        profiles = [spec.labels["cerebro.io/profile"]] if spec.labels.get("cerebro.io/profile") else []
        await self._run("up", "-d", spec.name, profiles=profiles)
        self.touch(UnitRef(name=spec.name))
        deadline = time.monotonic() + float(self.option("ready_timeout", 300))
        while True:
            st = await self.status(UnitRef(name=spec.name))
            if st.ready or time.monotonic() > deadline:
                return Endpoint(url=self.endpoint(spec.name), ready=st.ready)
            await asyncio.sleep(2)

    async def release(self, ref: UnitRef) -> None:
        await self._run("stop", ref.name)

    async def status(self, ref: UnitRef) -> UnitStatus:
        rows = parse_ps(await self._run("ps", "-a", "--format", "json", ref.name, profiles=["jobs"], check=False))
        running = [r for r in rows if r.get("State") == "running"]
        healthy = [r for r in running if r.get("Health", "") in ("", "healthy")]
        return UnitStatus(name=ref.name, exists=bool(rows), ready=bool(healthy), replicas=len(running),
                          last_used=self.last_used.get(ref.name),
                          message=", ".join(f"{r.get('Name')}: {r.get('Status')}" for r in rows) or "not created")

    async def run_job(self, job: JobSpec, *, wait: bool = False) -> str:
        if wait:
            await self._run("run", "--rm", job.name, profiles=["jobs"], capture=False)
            return job.name
        out = await self._run("run", "--rm", "-d", job.name, profiles=["jobs"])
        return out.strip() or job.name

    async def down(self, volumes: bool = False) -> None:
        args = ["down", "--remove-orphans"] + (["--volumes"] if volumes else [])
        await self._run(*args, profiles=["jobs"], capture=False)

    def touch(self, ref: UnitRef) -> None:
        self.last_used[ref.name] = datetime.now(timezone.utc)


def parse_ps(text: str) -> list[dict]:
    """`docker compose ps --format json`: one JSON object per line (v2.21+) or a JSON array (older)."""
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip().startswith("{")]

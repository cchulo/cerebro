import json, pathlib, shutil, subprocess
import pytest, yaml
from cerebro.adapters.provision import compose
from cerebro.core.contracts.provision import UnitRef
from cerebro.provision.plan import plan
from tests.conftest import ROOT
from tests.contracts.provision import ProvisionerContract
from tests.provision.fakes import CANARY, canary_ctx, fake_adapters, write_plugins


class TestComposeContract(ProvisionerContract):
    @pytest.fixture
    def adapter(self, ctx):
        return compose.Adapter({"output_dir": "deploy/generated"}, ctx)


@pytest.fixture
def rendered(example_config, tmp_path, monkeypatch):
    """(adapter, files, parsed compose document) for the fake plan, rendered from a fake checkout at tmp_path."""
    monkeypatch.chdir(tmp_path)
    shutil.copy(ROOT / "cerebro.example.yaml", tmp_path / "cerebro.yaml")
    write_plugins(tmp_path / "plugins")
    ctx = canary_ctx(example_config)
    units, jobs = plan(example_config, ctx, adapters=fake_adapters(ctx))
    adapter = compose.Adapter({"output_dir": "deploy/generated", "config_path": "cerebro.yaml"}, ctx)
    files = adapter.render(units, jobs)
    assert list(files) == ["deploy/generated/compose.yaml"]
    return adapter, files, yaml.safe_load(next(iter(files.values())))


def test_only_the_gateway_publishes_a_port(rendered):
    _, _, doc = rendered
    published = {n for n, s in doc["services"].items() if s.get("ports")}
    assert published == {"gateway"} and doc["services"]["gateway"]["ports"] == ["127.0.0.1:8090:8090"]


def test_secrets_are_env_file_references(rendered):
    _, files, doc = rendered
    svc = doc["services"]
    assert svc["docs-public"]["environment"]["POSTGRES_PASSWORD"] == "${POSTGRES_PASSWORD}"
    assert svc["docs-public"]["environment"]["MAX_ASYNC"] == "2"
    assert svc["memory"]["environment"]["HINDSIGHT_API_KEY"] == "${HINDSIGHT_API_KEY}"
    assert svc["memory"]["environment"]["HINDSIGHT_API_DATABASE_URL"] == "postgresql://cerebro:${POSTGRES_PASSWORD}@postgres:5432/hindsight"
    assert svc["gateway"]["environment"]["LIGHTRAG_API_KEY"] == "${LIGHTRAG_API_KEY}"
    assert CANARY not in next(iter(files.values())), "a secret value leaked into the rendered compose file"


def test_compose_value_escapes_everything_but_secret_refs():
    assert compose.compose_value("a$b ${X} $(y) ${Z:-q}") == "a$$b ${X} $$(y) $${Z:-q}"


def test_volumes_are_named_and_shared(rendered):
    _, _, doc = rendered
    svc = doc["services"]
    assert "code-public-repos:/repos" in svc["code-public"]["volumes"]
    assert "code-public-repos:/repos" in svc["index-code-public"]["volumes"]
    assert "docs-public-data:/app/data" in svc["docs-public"]["volumes"]
    assert doc["volumes"]["code-public-repos"]["labels"]["cerebro.io/scope"] == "public"
    assert doc["volumes"]["postgres-data"]["labels"]["cerebro.io/project"] == "cerebro"


def test_jobs_use_the_jobs_profile_and_scheduled_ones_get_a_crontab(rendered):
    _, _, doc = rendered
    svc = doc["services"]
    assert svc["index-code-public"]["profiles"] == ["jobs"] and svc["index-code-public"]["restart"] == "no"
    assert svc["index-code-public"]["command"] == ["index", "all"]
    assert svc["reindex-code-public"]["profiles"] == ["jobs"]
    crontab = doc["configs"]["scheduler-crontab"]["content"]
    assert "0 3 * * * " in crontab and "--profile jobs run --rm index-code-public" in crontab
    assert "reindex-code-public" not in crontab, "on-demand jobs have no cron line"
    sched = svc["scheduler"]
    assert sched["image"].startswith("docker:") and "/var/run/docker.sock:/var/run/docker.sock" in sched["volumes"]
    assert {"source": "scheduler-crontab", "target": "/etc/crontabs/root"} in sched["configs"]


def test_healthchecks_and_depends_on(rendered):
    _, _, doc = rendered
    svc = doc["services"]
    assert svc["postgres"]["healthcheck"]["test"][:2] == ["CMD", "pg_isready"]
    assert svc["ollama"]["healthcheck"]["test"] == ["CMD", "ollama", "list"]
    shell = svc["docs-public"]["healthcheck"]["test"]
    assert shell[0] == "CMD-SHELL" and all(t in shell[1] for t in ("curl", "wget", "python3", "http://localhost:9621/health"))
    assert "healthcheck" not in svc["mcp-fakemcp"]
    assert svc["gateway"]["depends_on"]["postgres"] == {"condition": "service_healthy"}
    assert svc["gateway"]["depends_on"]["docs-public"] == {"condition": "service_healthy"}
    assert svc["gateway"]["depends_on"]["mcp-fakemcp"] == {"condition": "service_started"}
    assert svc["docs-public"]["depends_on"] == {"postgres": {"condition": "service_healthy"}}


def test_files_become_inline_configs(rendered):
    _, _, doc = rendered
    pg = doc["services"]["postgres"]
    assert pg["configs"] == [{"source": "postgres-file-0", "target": "/docker-entrypoint-initdb.d/init.sql"}]
    assert "CREATE DATABASE keycloak;" in doc["configs"]["postgres-file-0"]["content"]


def test_labels_and_config_mounts(rendered, tmp_path):
    adapter, _, doc = rendered
    for name, s in doc["services"].items():
        assert s["labels"]["cerebro.io/project"] == "cerebro" and "cerebro.io/role" in s["labels"], name
    assert doc["services"]["docs-public"]["labels"]["cerebro.io/scope"] == "public"
    assert doc["services"]["docs-public"]["labels"]["cerebro.io/port"] == "9621"
    gw = doc["services"]["gateway"]
    assert "../../cerebro.yaml:/config/cerebro.yaml:ro" in gw["volumes"] and "../../plugins:/plugins:ro" in gw["volumes"]
    assert gw["build"] == {"context": "../../images/gateway"} and gw["image"] == "cerebro/gateway"
    assert "../..:/workspace:ro" in doc["services"]["scheduler"]["volumes"]
    assert doc["name"] == "cerebro" and doc["networks"]["default"]["labels"]["cerebro.io/project"] == "cerebro"


def test_endpoint_uses_rendered_ports(rendered):
    adapter, files, _ = rendered
    assert adapter.endpoint("docs-public") == "http://docs-public:9621" and adapter.endpoint("postgres") == "http://postgres:5432"
    for p, c in files.items():
        pathlib.Path(p).parent.mkdir(parents=True, exist_ok=True); pathlib.Path(p).write_text(c)
    fresh = compose.Adapter({"output_dir": "deploy/generated"}, adapter.ctx)
    assert fresh.endpoint("docs-public") == "http://docs-public:9621", "ports are recovered from the rendered file"
    assert fresh.endpoint("unknown") == "http://unknown:8080"


def test_command_line_and_profiles(rendered, tmp_path):
    adapter, _, _ = rendered
    cmd = adapter.command("up", "-d", "gateway")
    assert cmd == ["docker", "compose", "-p", "cerebro", "-f", "deploy/generated/compose.yaml", "up", "-d", "gateway"]
    (tmp_path / "secrets.env").write_text("POSTGRES_PASSWORD=x\n")
    cmd = adapter.command("run", "--rm", "index-code-public", profiles=["jobs"])
    assert cmd[5:] == ["deploy/generated/compose.yaml", "--env-file", "secrets.env", "--profile", "jobs", "run", "--rm", "index-code-public"]


def test_parse_ps_accepts_both_formats():
    assert compose.parse_ps("") == []
    assert compose.parse_ps('{"Service": "a", "State": "running"}\n{"Service": "b", "State": "exited"}\n')[1]["State"] == "exited"
    assert compose.parse_ps(json.dumps([{"Service": "a"}])) == [{"Service": "a"}]


async def test_ensure_release_status_and_touch_drive_docker_compose(rendered):
    adapter, _, _ = rendered
    calls = []

    async def fake_run(*args, profiles=(), check=True, capture=True):
        calls.append((args, tuple(profiles)))
        if args[0] == "ps":
            return json.dumps({"Service": args[-1], "Name": f"cerebro-{args[-1]}-1", "State": "running", "Health": "healthy", "Status": "Up"})
        return "abc123\n" if "-d" in args else ""
    adapter._run = fake_run
    unit = next(u for u in plan(adapter.ctx.config, adapter.ctx, adapters=fake_adapters(adapter.ctx))[0] if u.name == "docs-public")
    ep = await adapter.ensure(unit)
    assert ep.url == "http://docs-public:9621" and ep.ready
    assert calls[0] == (("up", "-d", "docs-public"), ())
    st = await adapter.status(UnitRef(name="docs-public"))
    assert st.exists and st.ready and st.replicas == 1 and st.last_used is not None, "ensure() touches the unit"
    await adapter.release(UnitRef(name="docs-public"))
    assert calls[-1] == (("stop", "docs-public"), ())
    job = next(j for j in plan(adapter.ctx.config, adapter.ctx, adapters=fake_adapters(adapter.ctx))[1])
    assert await adapter.run_job(job) == "abc123" and calls[-1] == (("run", "--rm", "-d", job.name), ("jobs",))
    await adapter.down(volumes=True)
    assert calls[-1] == (("down", "--remove-orphans", "--volumes"), ("jobs",))


def _docker_compose_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _docker_compose_available(), reason="docker compose not available")
def test_docker_compose_config_accepts_the_rendered_file(rendered, tmp_path):
    adapter, files, _ = rendered
    for p, c in files.items():
        pathlib.Path(p).parent.mkdir(parents=True, exist_ok=True); pathlib.Path(p).write_text(c)
    for d in ("images/gateway", "images/ingest", "images/code-unit"):
        (tmp_path / d).mkdir(parents=True)
    env = tmp_path / "secrets.env"
    env.write_text("".join(f"{k}={CANARY}-{k}\n" for k in [*adapter.ctx.config.secrets.keys, "FAKE_TOKEN", "CEREBRO_AUTH_ADMIN_PASSWORD"]))
    cmd = ["docker", "compose", "-p", "cerebro", "-f", str(adapter.compose_file), "--env-file", str(env), "--profile", "jobs", "config"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert f"{CANARY}-POSTGRES_PASSWORD" in res.stdout, "compose substitutes the env file at run time, not at render time"
    assert "variable is not set" not in res.stderr

import logging
import pytest
from cerebro.core import load_config, AdapterContext
from cerebro.core.contracts.provision import JobSpec, UnitSpec, VolumeSpec
from cerebro.provision import plan as planmod
from cerebro.provision.common import volume_key
from cerebro.provision.plan import plan, resolve_env_refs
from tests.conftest import ROOT
from tests.provision.fakes import FakeDocs, canary_ctx, fake_adapters, write_plugins


@pytest.fixture
def planned(example_config, tmp_path):
    ctx = canary_ctx(example_config)
    return plan(example_config, ctx, adapters=fake_adapters(ctx), plugins_dir=write_plugins(tmp_path / "plugins"))


def test_base_units_follow_the_conventions(planned, example_config):
    units, _ = planned
    by = {u.name: u for u in units}
    pg = by["postgres"]
    assert pg.role == "infra" and pg.image == "pgvector/pgvector:pg16" and pg.http_port == 5432 and pg.stateful
    assert pg.env["POSTGRES_USER"] == "cerebro" and pg.secret_env == ["POSTGRES_PASSWORD"]
    assert pg.health_cmd and pg.health_cmd[0] == "pg_isready" and pg.health_path is None
    sql = pg.files["/docker-entrypoint-initdb.d/init.sql"]
    for db in ("lightrag", "hindsight", "keycloak", "cerebro"):
        assert f"CREATE DATABASE {db};" in sql
    assert sql.count("CREATE EXTENSION IF NOT EXISTS vector") == 2
    assert by["ollama"].role == "inference" and by["ollama"].http_port == 11434 and by["ollama"].volumes
    gw = by["gateway"]
    assert gw.role == "gateway" and gw.build == "images/gateway" and gw.http_port == example_config.gateway.port
    assert gw.env["CEREBRO_CONFIG"] == "/config/cerebro.yaml" and gw.secret_env == example_config.secrets.keys
    assert {"postgres", "docs-public", "docs-payments", "code-public", "memory", "auth", "mcp-fakemcp"} <= set(gw.depends_on)
    ing = by["ingest"]
    assert ing.role == "ingest" and ing.http_port == 8080 and ing.volumes[0].name == "state"
    assert set(ing.depends_on) == {"postgres", "docs-public", "docs-payments", "docs-infra"}
    assert [u.name for u in units[:4]] == ["postgres", "ollama", "gateway", "ingest"], "base units first"


def test_ollama_only_when_inference_points_at_it(example_config):
    ctx = canary_ctx(example_config)
    cfg = load_config(ROOT / "cerebro.example.yaml", env={"LLM_BASE_URL": "http://models.internal:11434",
                                                          "EMBED_BASE_URL": "http://models.internal:11434"})
    units, _ = plan(cfg, ctx, adapters=[])
    assert "ollama" not in {u.name for u in units}
    units, _ = plan(example_config, ctx, adapters=[])
    assert "ollama" in {u.name for u in units}


def test_secret_references_move_into_secret_env(planned):
    units, jobs = planned
    by = {u.name: u for u in units}
    docs = by["docs-public"]
    assert docs.env["POSTGRES_PASSWORD"] == "${POSTGRES_PASSWORD}" and docs.env["LIGHTRAG_API_KEY"] == "${LIGHTRAG_API_KEY}"
    assert docs.env["MAX_ASYNC"] == "2", "${NAME:-default} with NAME not in secrets.keys becomes the literal default"
    assert docs.secret_env == ["POSTGRES_PASSWORD", "LIGHTRAG_API_KEY"]
    mem = by["memory"]
    assert mem.env["HINDSIGHT_API_DATABASE_URL"] == "postgresql://cerebro:${POSTGRES_PASSWORD}@postgres:5432/hindsight"
    assert mem.secret_env == ["HINDSIGHT_API_KEY", "POSTGRES_PASSWORD"]
    job = next(j for j in jobs if j.name == "index-code-public")
    assert job.secret_env == ["GITHUB_TOKEN"]


def test_resolve_env_refs_keeps_declared_defaults_as_secrets():
    env, names = resolve_env_refs({"A": "${X:-1}", "B": "${Y:-2}", "C": "x${Z}y", "D": "plain"}, ["X", "Z"])
    assert env == {"A": "${X}", "B": "2", "C": "x${Z}y", "D": "plain"} and names == ["X", "Z"]


def test_mcp_units_from_plugins(example_config, tmp_path):
    ctx = canary_ctx(example_config)
    pdir = write_plugins(tmp_path / "plugins")
    units, _ = plan(example_config, ctx, adapters=[], plugins_dir=pdir)
    mcp = next(u for u in units if u.name == "mcp-fakemcp")
    assert mcp.role == "mcp" and mcp.image == "ghcr.io/example/mcp:1.0" and mcp.http_port == 9000 and mcp.args == ["--stateless"]
    assert mcp.env == {"FAKE_URL": "http://fake.internal", "FAKE_TOKEN": "${FAKE_TOKEN}"}
    assert mcp.secret_env == ["FAKE_TOKEN"] and mcp.labels["cerebro.io/mcp-path"] == "/mcp"
    for live in ({"enabled": False}, {"via": "rest"}, {"url": "http://elsewhere:9000/mcp"}):
        example_config.sources["fakemcp"] = {"live": live}
        units, _ = plan(example_config, ctx, adapters=[], plugins_dir=pdir)
        assert "mcp-fakemcp" not in {u.name for u in units}, live
    example_config.sources.pop("fakemcp")


def test_missing_adapters_are_skipped_with_a_note(example_config, caplog):
    cfg = example_config.model_copy(deep=True)
    for kind in ("docs", "code", "memory"):
        getattr(cfg.engines, kind).type = "does-not-exist"
    cfg.identity.mode = "builtin"
    cfg.identity.server = cfg.identity.server or type(cfg.identity).model_fields["server"].annotation.__args__[0](type="no-such-auth")
    with caplog.at_level(logging.INFO, logger="cerebro.provision"):
        units, jobs = plan(cfg, AdapterContext(cfg), plugins_dir=str(ROOT / "does-not-exist"))
    assert [u.name for u in units] == ["postgres", "ollama", "gateway", "ingest"] and jobs == []
    skipped = [r.getMessage() for r in caplog.records if "skipping" in r.getMessage()]
    assert len(skipped) == 4 and any("no-such-auth" in m for m in skipped)


def test_duplicate_names_are_rejected(example_config):
    ctx = canary_ctx(example_config)
    with pytest.raises(ValueError, match="duplicate"):
        plan(example_config, ctx, adapters=[FakeDocs({}, ctx), FakeDocs({}, ctx)], plugins_dir=str(ROOT / "nope"))


def test_every_spec_carries_the_project_label(planned, example_config):
    units, jobs = planned
    for s in [*units, *jobs]:
        assert s.labels["cerebro.io/project"] == example_config.provisioning.project
    assert next(u for u in units if u.name == "docs-public").labels["cerebro.io/adapter"] == "docs:fakerag"


def test_volume_sharing_convention():
    unit = UnitSpec(name="code-public", role="code", image="x",
                    volumes=[VolumeSpec(name="repos", mount_path="/r", shared_with=["index-code-public"])])
    job = JobSpec(name="index-code-public", image="x", volumes=[VolumeSpec(name="repos", mount_path="/r", shared_with=["code-public"])])
    assert volume_key(unit, unit.volumes[0], {"code-public"}) == "code-public-repos"
    assert volume_key(job, job.volumes[0], {"code-public"}) == "code-public-repos"
    solo = UnitSpec(name="docs-public", role="docs", image="x", volumes=[VolumeSpec(name="data", mount_path="/d")])
    assert volume_key(solo, solo.volumes[0], {"docs-public"}) == "docs-public-data"


def test_init_sql_comes_from_deploy_base_file():
    assert "CREATE DATABASE keycloak;" in planmod.init_sql()

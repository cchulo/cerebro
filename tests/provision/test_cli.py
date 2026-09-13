import pathlib
import pytest, yaml
from cerebro import provision_cli
from tests.conftest import ROOT


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A fake checkout: cerebro.yaml with no engine adapters (only the base units), an empty plugins dir."""
    cfg = yaml.safe_load((ROOT / "cerebro.example.yaml").read_text())
    cfg["engines"] = {"docs": None, "code": None, "memory": None}
    (tmp_path / "cerebro.yaml").write_text(yaml.safe_dump(cfg))
    (tmp_path / "plugins").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_render_compose_writes_the_file(checkout, capsys):
    assert provision_cli.main(["render", "--target", "compose"]) == 0
    doc = yaml.safe_load((checkout / "deploy/generated/compose.yaml").read_text())
    assert set(doc["services"]) == {"postgres", "ollama", "gateway", "ingest"}
    assert "rendered 1 file(s)" in capsys.readouterr().out


def test_render_kubernetes_copies_the_env_file(checkout, capsys):
    (checkout / "secrets.env").write_text("POSTGRES_PASSWORD=x\n")
    assert provision_cli.main(["render", "--target", "kubernetes", "-c", "cerebro.yaml"]) == 0
    k = checkout / "deploy/generated/k8s"
    assert (k / "kustomization.yaml").exists() and (k / "postgres.yaml").exists() and (k / "namespace.yaml").exists()
    assert (k / "secrets.env").read_text() == "POSTGRES_PASSWORD=x\n"
    assert (k / "cerebro.yaml").read_text() == (checkout / "cerebro.yaml").read_text()
    assert "copied secrets.env" in capsys.readouterr().out


def test_plan_lists_units(checkout, capsys):
    assert provision_cli.main(["plan"]) == 0
    out = capsys.readouterr().out
    assert "unit postgres" in out and "unit gateway" in out and "image=pgvector/pgvector:pg16" in out


def test_unknown_unit_or_job_is_an_error(checkout):
    with pytest.raises(SystemExit, match="unknown job"):
        provision_cli.main(["job", "nope"])
    assert provision_cli._selected([], []) == []
    with pytest.raises(SystemExit, match="unknown unit"):
        provision_cli._selected([], ["x"])

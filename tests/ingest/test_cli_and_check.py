"""`cerebro ingest check|sync` against a config file on disk; the docs engine is the FakeIndex by module:Class."""
import json
import yaml
from cerebro.cli import main as cerebro_main
from cerebro.ingest.check import check
from cerebro.ingest.cli import main
from tests.ingest.conftest import ROOT


def test_check_lists_what_a_plugin_yields(ingest):
    lines: list[str] = []
    assert check(ingest, "public", "files", limit=2, out=lines.append) == 0
    text = "\n".join(lines)
    assert "available sources: backstage (plugin backstage.py)" in text and "files (plugin files.py)" in text
    assert "files: configured=True" in text and "- public/adr/0001.md" in text and "4 documents" in text
    assert "(showing first 2)" in text and "public/onboarding.txt" not in text
    assert check(ingest, "nope", "files", out=lines.append) == 1
    assert check(ingest, "public", "confluence", out=lines.append) == 1
    assert "does not list source confluence" in lines[-1]


def test_check_full_and_string_filter(ingest):
    lines: list[str] = []
    assert check(ingest, "infra", "files", filter='{"x": 1}', full=True, out=lines.append) == 0
    assert "Restart with docker compose restart postgres." in "\n".join(lines)


def _write_config(tmp_path, docs_dir):
    cfg = {"gateway": {"plugins_dir": str(ROOT / "plugins")},
           "engines": {"docs": {"type": "tests.ingest.conftest:FakeIndex"}},
           "scopes": {"public": {"groups": ["everyone"], "docs": {"files": {"paths": [str(docs_dir / "public")]}}}}}
    p = tmp_path / "cerebro.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


def test_cli_sync_and_check(tmp_path, docs_dir, capsys, monkeypatch):
    p = _write_config(tmp_path, docs_dir)
    monkeypatch.setenv("CEREBRO_STATE_DIR", str(tmp_path / "state"))
    assert main(["-c", str(p), "sync", "--scope", "public"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["public/files"] == {"changed": 4, "removed": 0} and report["public"]["index"]["inserted"] == 4
    assert (tmp_path / "state" / "versions.json").exists()
    assert cerebro_main(["ingest", "-c", str(p), "sync", "files"]) == 0            # through the top-level entry point
    assert json.loads(capsys.readouterr().out)["public/files"] == {"changed": 0, "removed": 0}
    assert main(["-c", str(p), "check", "public", "files", "--limit", "1"]) == 0
    assert "4 documents" in capsys.readouterr().out
    assert main(["-c", str(p), "check", "public", "git"]) == 1

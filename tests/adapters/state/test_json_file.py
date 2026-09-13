import json, os
import pytest
from cerebro.core import registry
from cerebro.adapters.state.json_file import Adapter
from tests.contracts.state import SyncStateContract


class TestJsonFileContract(SyncStateContract):
    @pytest.fixture
    def adapter(self, ctx, tmp_path):
        return Adapter({"dir": str(tmp_path)}, ctx)


def test_path_options_and_env_default(ctx, tmp_path, monkeypatch):
    assert Adapter({"path": "/x/versions.json"}, ctx).path == "/x/versions.json"
    assert Adapter({"dir": "/y"}, ctx).path == "/y/versions.json"
    monkeypatch.setenv("CEREBRO_STATE_DIR", str(tmp_path / "s"))
    a = registry.build("state", "json_file", {}, ctx)
    assert a.path == str(tmp_path / "s" / "versions.json")
    a.commit({"k": "1"})
    assert json.loads((tmp_path / "s" / "versions.json").read_text()) == {"k": "1"}
    monkeypatch.delenv("CEREBRO_STATE_DIR")
    assert Adapter({}, ctx).path == os.path.join("./state", "versions.json")


def test_commit_is_atomic_and_survives_reopen(ctx, tmp_path):
    a = Adapter({"dir": str(tmp_path)}, ctx)
    a.commit({"a:s:1": "v1", "a:s:2": "v1"})
    b = Adapter({"dir": str(tmp_path)}, ctx)                 # a fresh instance sees the same file
    assert b.get("a:s:2") == "v1" and not (tmp_path / "versions.json.tmp").exists()
    b.commit({}, delete_keys=["a:s:1", "never-there"])
    assert a.keys_with_prefix("a:") == ["a:s:2"]

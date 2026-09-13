"""End to end with the REAL TokenSave binary: `cerebro index run` on a local bare repo with two branches, then the
bridge with TokenSaveEngine, queried over streamable HTTP. Skipped unless `tokensave` is on PATH, $TOKENSAVE_BIN
is set, or pytest is given `--tokensave /path/to/tokensave`."""
import json, os, pathlib, shutil, subprocess
import httpx, pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from cerebro.adapters.code import index_cli
from cerebro.bridge.engine import TokenSaveEngine
from cerebro.bridge.workspace import RepoState, Workspace, entries_from_env
from tests.workspace_fixture import git, make_repo, running_bridge


@pytest.fixture
def tokensave(request, tmp_path, monkeypatch):
    path = request.config.getoption("--tokensave") or os.environ.get("TOKENSAVE_BIN") or shutil.which("tokensave")
    if not path:
        pytest.skip("real tokensave binary not available (pass --tokensave PATH or set TOKENSAVE_BIN)")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))          # TokenSave's global state stays in the sandbox
    monkeypatch.setenv("TOKENSAVE_UPDATE_CHECK", "off")
    return path


async def test_index_then_serve_with_real_tokensave(tmp_path, tokensave):
    src = make_repo(tmp_path / "src", "alpha", {"src/app.py": "def main():\n    return helper()\n\n\ndef helper():\n    return 1\n"},
                    {"feature": {"src/app.py": "def main():\n    return helper()\n\n\ndef helper():\n    return 2\n\n\ndef only_on_feature():\n    pass\n"}},
                    indexed=False)
    bare = tmp_path / "origin" / "alpha.git"; bare.parent.mkdir()
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    ws_root = tmp_path / "ws"
    repos = json.dumps([{"url": str(bare), "branches": ["feature"]}])
    assert index_cli.main(["run", "--unit", "code-real", "--workspace", str(ws_root), "--repos", repos,
                           "--tokensave", tokensave, "--log-level", "warning"]) == 0
    d = ws_root / "alpha"
    assert (d / ".tokensave" / "tokensave.db").exists() and (d / ".tokensave" / "branches" / "feature.db").exists()
    assert (ws_root / ".cerebro-root" / ".tokensave").exists()
    state = RepoState.model_validate_json((d / ".cerebro-index.json").read_text())
    assert set(state.branches) == {"main", "feature"} and git(d, "symbolic-ref", "--short", "HEAD") == "main"
    # second run: nothing moved, nothing re-synced (state carries the commits)
    assert index_cli.main(["run", "--unit", "code-real", "--workspace", str(ws_root), "--repos", repos,
                           "--tokensave", tokensave, "--log-level", "warning"]) == 0

    ws = Workspace(ws_root, entries_from_env(repos))
    async with running_bridge("code-real", ws, TokenSaveEngine(ws, binary=tokensave)) as (bridge, url):
        async with httpx.AsyncClient() as h:
            m = (await h.get(f"{url}/.well-known/cerebro-capabilities")).json()
            assert (await h.get(f"{url}/health")).json()["engine_alive"]
        names = {t["name"] for t in m["tools"]}
        assert m["engine"] == "tokensave" and m["version"].startswith("7.") and {"grep", "unit_info", "tokensave_search", "tokensave_callers"} <= names
        assert not any(n.startswith(("tokensave_str_replace", "tokensave_session_", "tokensave_record_", "tokensave_branch_")) for n in names)
        assert all(t["read_only"] for t in m["tools"]) and len(names) > 40
        async with streamablehttp_client(f"{url}/mcp") as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool("tokensave_search", {"query": "only_on_feature", "repo": "alpha"})
                assert not res.isError and "only_on_feature" not in "".join(c.text for c in res.content if c.text and c.text.startswith("["))
                res = await s.call_tool("tokensave_search", {"query": "only_on_feature", "repo": "alpha", "branch": "feature"})
                text = "\n".join(c.text for c in res.content)
                assert not res.isError and '"name": "only_on_feature"' in text and 'branch="feature"' in text
                res = await s.call_tool("tokensave_search", {"query": "x", "repo": "alpha", "branch": "nope"})
                assert res.isError
                res = await s.call_tool("grep", {"query": "only_on_feature", "branch": "feature"})
                assert res.structuredContent["hits"][0]["path"] == "src/app.py"
                res = await s.call_tool("tokensave_str_replace", {"repo": "alpha", "path": "src/app.py", "old_str": "1", "new_str": "2"})
                assert res.isError

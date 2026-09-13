"""The wire contract of the bridge, against the fake stdio engine and real git repositories."""
import json
import httpx, pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from cerebro.bridge import cli as bridge_cli
from cerebro.bridge.workspace import Workspace, repo_dir_names
from tests.workspace_fixture import entries


async def call(url: str, tool: str, args: dict | None = None):
    async with streamablehttp_client(f"{url}/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await s.call_tool(tool, args or {})


async def list_tools(url: str):
    async with streamablehttp_client(f"{url}/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return {t.name: t for t in (await s.list_tools()).tools}


# ----------------------------------------------------------------------------- manifest / health
async def test_manifest_and_health(bridge):
    _, url = bridge
    async with httpx.AsyncClient() as h:
        m = (await h.get(f"{url}/.well-known/cerebro-capabilities")).json()
        hl = await h.get(f"{url}/health")
    assert m["engine"] == "fake" and m["unit"] == "code-test" and m["version"] == "0.0-fake"
    assert m["capabilities"] == {"search": True, "graph": True, "branches": True, "multi_root": True}
    names = {t["name"] for t in m["tools"]}
    assert {"grep", "unit_info", "fake_search", "fake_status", "fake_fail"} <= names
    assert "fake_write" not in names and "fake_local_only" not in names, "write tools and selector-less tools stay hidden"
    assert all(t["read_only"] for t in m["tools"])
    assert [r["name"] for r in m["repos"]] == ["github.com/acme/alpha", "github.com/acme/beta", "github.com/acme/gamma"]
    alpha = m["repos"][0]
    assert alpha["dir"] == "alpha" and alpha["default_branch"] == "main" and set(alpha["indexed"]) == {"main", "feature", "release/1.0"}
    assert hl.status_code == 200 and hl.json()["ok"] and hl.json()["engine_alive"] and hl.json()["indexed_repos"] == 2


async def test_tools_over_mcp_have_repo_and_branch_not_selectors(bridge):
    _, url = bridge
    tools = await list_tools(url)
    props = tools["fake_search"].inputSchema["properties"]
    assert "repo" in props and "branch" in props and "graph_root" not in props and "graph_branch" not in props
    assert "query" in props and tools["fake_search"].annotations.readOnlyHint is True
    assert "fake_write" not in tools and "fake_local_only" not in tools


# ----------------------------------------------------------------------------- selector injection
async def test_call_injects_graph_root_and_strips_repo(bridge, workspace):
    _, url = bridge
    res = await call(url, "fake_search", {"query": "helper", "repo": "github.com/acme/alpha"})
    assert not res.isError
    args = res.structuredContent["args"]
    assert args["graph_root"] == str(workspace.root / "alpha") and args["graph_branch"] is None and args["query"] == "helper"


async def test_call_accepts_dir_and_url_as_repo(bridge, workspace):
    _, url = bridge
    for ref in ("alpha", "https://github.com/acme/alpha.git", "https://github.com/acme/alpha"):
        res = await call(url, "fake_status", {"repo": ref})
        assert not res.isError and res.structuredContent["args"]["graph_root"] == str(workspace.root / "alpha")


async def test_branch_maps_to_graph_branch_only_when_not_default(bridge):
    _, url = bridge
    res = await call(url, "fake_search", {"query": "x", "repo": "alpha", "branch": "feature"})
    assert res.structuredContent["args"]["graph_branch"] == "feature"
    res = await call(url, "fake_search", {"query": "x", "repo": "alpha", "branch": "main"})
    assert res.structuredContent["args"]["graph_branch"] is None, "the default branch needs no selector"
    res = await call(url, "fake_search", {"query": "x", "repo": "alpha", "branch": "release/1.0"})
    assert res.structuredContent["args"]["graph_branch"] == "release/1.0"


async def test_caller_supplied_selectors_are_overridden(bridge, workspace):
    _, url = bridge
    res = await call(url, "fake_search", {"query": "x", "repo": "beta", "graph_root": "/etc", "graph_branch": "evil"})
    assert res.structuredContent["args"]["graph_root"] == str(workspace.root / "beta")
    assert res.structuredContent["args"]["graph_branch"] is None


async def test_errors_are_tool_errors(bridge):
    _, url = bridge
    assert (await call(url, "fake_search", {"query": "x", "repo": "github.com/other/repo"})).isError
    r = await call(url, "fake_search", {"query": "x", "repo": "alpha", "branch": "nope"})
    assert r.isError and "not indexed" in r.content[0].text
    assert (await call(url, "fake_write", {"path": "x", "content": "y"})).isError
    assert (await call(url, "fake_local_only", {})).isError
    r = await call(url, "fake_fail", {"repo": "alpha"})
    assert r.isError and "boom" in r.content[0].text
    r = await call(url, "fake_search", {"query": "x", "repo": "gamma"})
    assert r.isError and "not indexed" in r.content[0].text


async def test_repo_omitted_fans_out_over_every_repo(bridge, workspace):
    _, url = bridge
    res = await call(url, "fake_search", {"query": "q"})
    results = res.structuredContent["results"]
    assert [r["repository"] for r in results] == ["github.com/acme/alpha", "github.com/acme/beta", "github.com/acme/gamma"]
    assert results[0]["structured"]["args"]["graph_root"] == str(workspace.root / "alpha")
    assert results[1]["structured"]["args"]["graph_root"] == str(workspace.root / "beta")
    assert results[2]["is_error"] and not res.isError, "one un-indexed repo does not fail the whole call"


# ----------------------------------------------------------------------------- grep
async def test_grep_default_branch_with_ripgrep(bridge):
    _, url = bridge
    res = await call(url, "grep", {"query": "def main", "context": 0})
    hits = res.structuredContent["hits"]
    assert {(h["repository"], h["path"]) for h in hits} == {("github.com/acme/alpha", "src/app.py"), ("github.com/acme/beta", "b.py")}
    a = next(h for h in hits if h["repository"] == "github.com/acme/alpha")
    assert a["line"] == 1 and a["content"] == "def main():" and a["language"] == "python" and a["branch"] is None
    assert res.structuredContent["errors"] == ["github.com/acme/gamma: not checked out yet"] or res.structuredContent["errors"] == []


async def test_grep_regex_context_and_limits(bridge):
    _, url = bridge
    res = await call(url, "grep", {"query": r"def (main|helper)", "regex": True, "repos": ["alpha"], "context": 1})
    hits = res.structuredContent["hits"]
    assert [h["line"] for h in hits] == [1, 5] and "return helper()" in hits[0]["content"]
    res = await call(url, "grep", {"query": "def", "max_results": 1})
    assert len(res.structuredContent["hits"]) == 1 and res.structuredContent["truncated"]
    res = await call(url, "grep", {"query": "def (main", "regex": False, "repos": ["alpha"]})
    assert res.structuredContent["hits"] == [], "a fixed string is not a regex"


async def test_grep_other_branch_uses_git_grep(bridge):
    _, url = bridge
    res = await call(url, "grep", {"query": "only_on_feature", "branch": "feature"})
    hits = res.structuredContent["hits"]
    assert len(hits) == 1 and hits[0]["repository"] == "github.com/acme/alpha" and hits[0]["branch"] == "feature"
    assert hits[0]["path"] == "src/app.py" and hits[0]["line"] == 9 and "def only_on_feature" in hits[0]["content"]
    assert any("beta" in e for e in res.structuredContent["errors"]), "beta has no feature branch"
    res = await call(url, "grep", {"query": "def main", "branch": "feature", "repos": ["alpha"], "context": 1})
    assert res.structuredContent["hits"][0]["content"].startswith("def main():\n    return helper()")
    res = await call(url, "grep", {"query": "REL", "branch": "release/1.0", "repos": ["alpha"]})
    assert res.structuredContent["hits"][0]["path"] == "rel.py"
    res = await call(url, "grep", {"query": "x", "branch": "untracked"})
    assert res.structuredContent["hits"] == [] and res.structuredContent["errors"]


async def test_grep_repos_filter_never_widens(bridge):
    _, url = bridge
    res = await call(url, "grep", {"query": "def", "repos": ["github.com/other/x", "beta"]})
    assert {h["repository"] for h in res.structuredContent["hits"]} == {"github.com/acme/beta"}
    res = await call(url, "grep", {"query": "def", "repos": ["github.com/other/x"]})
    assert res.structuredContent["hits"] == []
    assert (await call(url, "grep", {"query": ""})).isError


async def test_unit_info(bridge):
    _, url = bridge
    res = await call(url, "unit_info", {})
    assert res.structuredContent["unit"] == "code-test" and res.structuredContent["repos"][0]["dir"] == "alpha"


# ----------------------------------------------------------------------------- layout and cli
def test_repo_dir_names_use_basename_unless_shared():
    assert repo_dir_names(["github.com/a/x", "github.com/b/y"]) == {"github.com/a/x": "x", "github.com/b/y": "y"}
    dup = repo_dir_names(["github.com/a/api", "github.com/b/api", "github.com/c/z"])
    assert dup["github.com/c/z"] == "z" and dup["github.com/a/api"] != dup["github.com/b/api"]
    assert all(v != "api" for k, v in dup.items() if k.endswith("/api"))


def test_workspace_select_and_entry(tmp_path):
    ws = Workspace(tmp_path, entries(("https://github.com/acme/alpha.git", ["main"]), ("git@github.com:acme/beta.git", [])))
    assert ws.entry("github.com/acme/beta").dir == "beta"
    assert [e.dir for e in ws.select(["beta", "nope", "github.com/acme/alpha", "beta"])] == ["beta", "alpha"]
    assert ws.select(None) == ws.repos and ws.select([]) == []
    with pytest.raises(KeyError):
        ws.entry("github.com/acme/nope")


def test_cli_builds_bridge_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CEREBRO_UNIT", "code-x")
    monkeypatch.setenv("CEREBRO_REPOS", json.dumps([{"url": "https://github.com/acme/alpha.git", "branches": ["main"]}, "https://github.com/acme/beta.git"]))
    args = bridge_cli.parser().parse_args(["serve", "--workspace", str(tmp_path), "--engine", "tests.bridge.fake_engine:FakeEngine"])
    b = bridge_cli.make_bridge(args)
    assert b.unit == "code-x" and [r.dir for r in b.workspace.repos] == ["alpha", "beta"] and b.engine.name == "fake"
    args = bridge_cli.parser().parse_args(["serve", "--unit", "u", "--workspace", str(tmp_path)])
    assert bridge_cli.make_bridge(args).engine.name == "tokensave"

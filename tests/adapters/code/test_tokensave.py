"""The TokenSave adapter: the contract harness against a real in-process bridge (fake stdio engine, real git
repositories), respx for the manifest / health paths, and the UnitSpec / JobSpec it hands the provisioner."""
import json
import httpx, pytest, respx
from cerebro.core import AdapterContext, CodeUnit, Health, StaticLocator, load_config
from cerebro.core.config import RepoSpec
from cerebro.core.contracts.code import Capabilities, ToolResult
from cerebro.core.types import Forbidden, Unsupported
from cerebro.core.registry import build, resolve
from cerebro.adapters.code.tokensave import Adapter
from cerebro.bridge.workspace import Workspace
from tests.contracts.code import CodeIntelligenceContract
from tests.bridge.fake_engine import FakeEngine
from tests.workspace_fixture import entries, make_repo, running_bridge

CLICK = {"src/click/core.py": "def main():\n    return 1\n", "README.md": "click\n"}
FLASK = {"src/flask/cli.py": "def main():\n    pass\n\n\ndef helper():\n    pass\n"}
FLASK_STABLE = {"src/flask/cli.py": "def main():\n    pass\n\n\ndef only_on_stable():\n    pass\n"}


def make_ctx(config, urls: dict[str, str]) -> AdapterContext:
    class Secrets:
        def get(self, name, default=None): return default
    return AdapterContext(config, secrets=Secrets(), locator=StaticLocator(urls, template="http://{unit}:8045"))


@pytest.fixture
def unit() -> CodeUnit:
    return CodeIntelligenceContract.unit.__wrapped__(None)


@pytest.fixture
def unit_workspace(tmp_path):
    root = tmp_path / "ws"; root.mkdir()
    make_repo(root, "click", CLICK, url="https://github.com/pallets/click.git", name="github.com/pallets/click")
    make_repo(root, "flask", FLASK, {"stable": FLASK_STABLE}, url="https://github.com/pallets/flask.git", name="github.com/pallets/flask")
    return Workspace(root, entries(("https://github.com/pallets/click.git", []), ("https://github.com/pallets/flask.git", ["main", "stable"])))


@pytest.fixture
async def live(unit_workspace, example_config):
    async with running_bridge("code-public", unit_workspace, FakeEngine(unit_workspace)) as (bridge, url):
        yield Adapter({}, make_ctx(example_config, {"code-public": url})), bridge, url


# ----------------------------------------------------------------------------- the contract harness
class TestTokenSaveContract(CodeIntelligenceContract):
    @pytest.fixture
    def adapter(self, live):
        return live[0]


# ----------------------------------------------------------------------------- end to end through the bridge
async def test_search_maps_grep_hits(live, unit):
    adapter, _, _ = live
    hits = await adapter.search(unit, "def main", max_results=10)
    assert {(h.repository, h.path, h.line) for h in hits} == {("github.com/pallets/click", "src/click/core.py", 1),
                                                              ("github.com/pallets/flask", "src/flask/cli.py", 1)}
    assert all(h.language == "python" and h.branch is None for h in hits)
    hits = await adapter.search(unit, "only_on_stable", branch="stable")
    assert [(h.repository, h.branch) for h in hits] == [("github.com/pallets/flask", "stable")]
    hits = await adapter.search(unit, r"def (main|helper)", regex=True, repos=["github.com/pallets/flask", "github.com/nope/x"])
    assert [h.line for h in hits] == [1, 5] and all(h.repository == "github.com/pallets/flask" for h in hits)
    assert await adapter.search(unit, "def", repos=["github.com/nope/x"]) == []


async def test_call_proxies_with_repo_and_branch(live, unit):
    adapter, _, _ = live
    r = await adapter.call(unit, "fake_search", {"query": "q", "repo": "github.com/pallets/flask"}, branch="stable")
    assert isinstance(r, ToolResult) and r.unit == "code-public" and r.tool == "fake_search" and not r.is_error
    args = r.structured["args"]
    assert args["graph_root"].endswith("/flask") and args["graph_branch"] == "stable" and "repo" not in args
    r = await adapter.call(unit, "fake_status", {})
    assert [x["repository"] for x in r.structured["results"]] == ["github.com/pallets/click", "github.com/pallets/flask"]
    r = await adapter.call(unit, "fake_search", {"query": "q", "repo": "github.com/pallets/click"}, branch="nope")
    assert r.is_error and "not indexed" in r.content[0]
    with pytest.raises(Forbidden):
        await adapter.call(unit, "fake_write", {"path": "x", "content": "y"})
    with pytest.raises(Forbidden):
        await adapter.call(unit, "fake_search", {"query": "q", "repo": "github.com/other/x"})


async def test_capabilities_and_health_live(live, unit):
    adapter, _, _ = live
    c = await adapter.capabilities(unit)
    assert c.engine == "fake" and c.search and c.graph and c.branches and c.multi_root
    assert {"grep", "unit_info", "fake_search"} <= c.tool_names() and "fake_write" not in c.tool_names()
    assert await adapter.capabilities(unit) is c, "cached per unit"
    h = await adapter.health(unit)
    assert h.ok and h.data["engine_alive"] and h.data["unit"] == "code-public"


# ----------------------------------------------------------------------------- respx: manifest and health paths
MANIFEST = {"engine": "tokensave", "version": "7.12.1", "unit": "code-public",
            "capabilities": {"search": True, "graph": True, "branches": False, "multi_root": True},
            "tools": [{"name": "grep", "description": "", "read_only": True, "input_schema": {}},
                      {"name": "tokensave_search", "description": "", "read_only": True, "input_schema": {}},
                      {"name": "tokensave_str_replace", "description": "", "read_only": False, "input_schema": {}}]}


@pytest.fixture
def adapter(example_config):
    return Adapter({}, make_ctx(example_config, {}))


@respx.mock
async def test_manifest_parsing_and_branch_gate(adapter, unit):
    respx.get("http://code-public:8045/.well-known/cerebro-capabilities").mock(return_value=httpx.Response(200, json=MANIFEST))
    c = await adapter.capabilities(unit)
    assert isinstance(c, Capabilities) and c.engine == "tokensave" and c.version == "7.12.1" and not c.branches
    assert c.tool_names() == {"grep", "tokensave_search"}, "non-read-only tools are dropped even if listed"
    with pytest.raises(Unsupported):
        await adapter.search(unit, "x", branch="stable")
    with pytest.raises(Unsupported):
        await adapter.call(unit, "tokensave_search", {"query": "x"}, branch="stable")
    with pytest.raises(Forbidden):
        await adapter.call(unit, "tokensave_str_replace", {})
    adapter.invalidate(unit)
    respx.get("http://code-public:8045/.well-known/cerebro-capabilities").mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.capabilities(unit)


@respx.mock
async def test_health_paths(adapter, unit):
    route = respx.get("http://code-public:8045/health")
    route.mock(return_value=httpx.Response(200, json={"ok": True, "engine": "tokensave", "version": "7.12.1", "engine_alive": True}))
    h = await adapter.health(unit)
    assert isinstance(h, Health) and h.ok and h.data["version"] == "7.12.1"
    route.mock(return_value=httpx.Response(503, json={"ok": False, "engine_alive": False}))
    assert not (await adapter.health(unit)).ok
    route.mock(side_effect=httpx.ConnectError("refused"))
    h = await adapter.health(unit)
    assert not h.ok and "ConnectError" in h.detail


# ----------------------------------------------------------------------------- what the provisioner gets
def test_units_and_jobs_from_example_config(example_config):
    adapter = build("code", "tokensave", {"schedule": "15 2 * * *"}, make_ctx(example_config, {}))
    assert resolve("code", "tokensave") is Adapter and adapter.name == "tokensave"
    units = {u.name: u for u in adapter.units()}
    jobs = {j.name: j for j in adapter.jobs()}
    assert set(units) == {"code-public", "code-infra"} | {n for n in units if n.startswith("code-payments-")}
    u = units["code-public"]
    assert u.role == "code" and u.image.startswith("cerebro/code-unit:") and u.build == "images/code-unit"
    assert u.args == ["cerebro", "bridge", "serve", "--unit", "code-public", "--workspace", "/workspace", "--port", "8045"]
    assert u.env["CEREBRO_UNIT"] == "code-public" and u.secret_env == ["GITHUB_TOKEN"] and u.http_port == 8045
    assert json.loads(u.env["CEREBRO_REPOS"]) == [{"url": "https://github.com/pallets/click.git", "branches": []},
                                                  {"url": "https://github.com/pallets/flask.git", "branches": ["main", "stable"]}]
    assert u.idle_ttl == "2h" and u.scope == "public" and u.depends_on == [] and u.health_path == "/health"
    assert [v.name for v in u.volumes] == ["workspace-code-public"] and u.volumes[0].mount_path == "/workspace"
    assert set(u.volumes[0].shared_with) == {"code-public", "index-code-public"}
    j = jobs["index-code-public"]
    assert j.image == u.image and j.args == ["cerebro", "index", "run", "--unit", "code-public"] and j.schedule == "15 2 * * *"
    assert j.env == u.env and j.secret_env == ["GITHUB_TOKEN"] and j.volumes == u.volumes and j.scope == "public"
    assert set(jobs) == {f"index-{n}" for n in units}
    assert Adapter({}, make_ctx(example_config, {})).jobs()[0].schedule == "0 3 * * *"


def test_image_registry_prefix_and_storage(tmp_path, example_config):
    cfg = example_config.model_copy(deep=True)
    cfg.provisioning.image_registry = "registry.internal/"
    cfg.engines.code.resources = {"cpu": "1", "memory": "2Gi", "storage": "50Gi"}
    u = Adapter({}, make_ctx(cfg, {})).units()[0]
    assert u.image.startswith("registry.internal/cerebro/code-unit:") and u.resources == {"cpu": "1", "memory": "2Gi"}
    assert u.volumes[0].size == "50Gi"

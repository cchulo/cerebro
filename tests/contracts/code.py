import pytest
from cerebro.core import Health, CodeUnit
from cerebro.core.config import RepoSpec
from cerebro.core.contracts import CodeIntelligence, Capabilities, ToolResult, SearchHit
from cerebro.core.types import Unsupported, Forbidden


class CodeIntelligenceContract:
    @pytest.fixture
    def unit(self) -> CodeUnit:
        return CodeUnit(name="code-public", scope="public", kind="scope",
                        repos=[RepoSpec(url="https://github.com/pallets/click.git"),
                               RepoSpec(url="https://github.com/pallets/flask.git", branches=["main", "stable"])])

    @pytest.fixture
    def adapter(self, ctx) -> CodeIntelligence:
        raise NotImplementedError

    def test_is_code_intelligence(self, adapter):
        assert isinstance(adapter, CodeIntelligence) and adapter.kind == "code"

    async def test_capabilities_shape(self, adapter, unit):
        c = await adapter.capabilities(unit)
        assert isinstance(c, Capabilities) and c.engine and (c.search or c.graph)
        assert all(t.read_only for t in c.tools), "only read-only tools may be exposed"

    async def test_health_shape(self, adapter, unit):
        assert isinstance(await adapter.health(unit), Health)

    async def test_search_returns_hits_inside_unit(self, adapter, unit):
        c = await adapter.capabilities(unit)
        if not c.search:
            pytest.skip("no search capability")
        hits = await adapter.search(unit, "def main", max_results=5)
        allowed = {r.name for r in unit.repos}
        assert all(isinstance(h, SearchHit) and h.repository in allowed for h in hits)

    async def test_search_repo_filter_never_widens(self, adapter, unit):
        c = await adapter.capabilities(unit)
        if not c.search:
            pytest.skip("no search capability")
        hits = await adapter.search(unit, "def main", repos=["github.com/pallets/click"], max_results=5)
        assert all(h.repository == "github.com/pallets/click" for h in hits)

    async def test_unknown_tool_is_forbidden(self, adapter, unit):
        c = await adapter.capabilities(unit)
        if not c.graph:
            pytest.skip("no graph capability")
        with pytest.raises((Forbidden, Unsupported)):
            await adapter.call(unit, "delete_everything", {})

    async def test_branch_without_capability_is_unsupported(self, adapter, unit):
        c = await adapter.capabilities(unit)
        if c.branches:
            pytest.skip("engine supports branches")
        with pytest.raises(Unsupported):
            await adapter.search(unit, "x", branch="stable")

    async def test_call_result_shape(self, adapter, unit):
        c = await adapter.capabilities(unit)
        if not c.graph or not c.tools:
            pytest.skip("no graph tools")
        r = await adapter.call(unit, c.tools[0].name, {})
        assert isinstance(r, ToolResult) and r.unit == unit.name

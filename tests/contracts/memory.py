import pytest
from cerebro.core import Health
from cerebro.core.contracts import MemoryStore, RecallResult, RetainResult, ReflectResult
from cerebro.core.types import Unsupported


class MemoryStoreContract:
    bank = "user-alice"

    @pytest.fixture
    def adapter(self, ctx) -> MemoryStore:
        raise NotImplementedError

    def test_is_memory_store(self, adapter):
        assert isinstance(adapter, MemoryStore) and adapter.kind == "memory"

    def test_budget_check(self, adapter):
        assert adapter.check_budget("mid") == "mid"
        with pytest.raises(ValueError):
            adapter.check_budget("huge")

    async def test_health_shape(self, adapter):
        assert isinstance(await adapter.health(), Health)

    async def test_recall_shape(self, adapter):
        r = await adapter.recall(self.bank, "checkout deploys")
        assert isinstance(r, RecallResult) and r.bank == self.bank

    async def test_retain_shape(self, adapter):
        r = await adapter.retain(self.bank, "deployed 2.3.1; rollback flag in the runbook was wrong", tags=["deploy"])
        assert isinstance(r, RetainResult) and r.bank == self.bank

    async def test_reflect_shape_or_unsupported(self, adapter):
        if not adapter.supports_reflect:
            with pytest.raises(Unsupported):
                await adapter.reflect(self.bank, "what do we know?")
            return
        r = await adapter.reflect(self.bank, "what do we know?")
        assert isinstance(r, ReflectResult) and r.bank == self.bank

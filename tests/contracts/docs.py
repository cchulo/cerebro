import pytest
from cerebro.core import Health
from cerebro.core.contracts import DocumentIndex, Batch, ApplyReport, DocAnswer, QueryOptions


class DocumentIndexContract:
    scope = "public"

    @pytest.fixture
    def adapter(self, ctx) -> DocumentIndex:                 # override: adapter against a mocked engine
        raise NotImplementedError

    def test_is_document_index(self, adapter):
        assert isinstance(adapter, DocumentIndex) and adapter.kind == "docs"
        assert adapter.modes and adapter.default_mode in adapter.modes

    def test_mode_check(self, adapter):
        assert adapter.check_mode(None) == adapter.default_mode
        with pytest.raises(ValueError):
            adapter.check_mode("no-such-mode")

    async def test_health_shape(self, adapter):
        assert isinstance(await adapter.health(self.scope), Health)

    async def test_query_shape(self, adapter):
        a = await adapter.query(self.scope, "how do we deploy?", QueryOptions())
        assert isinstance(a, DocAnswer) and a.scope == self.scope and isinstance(a.answered, bool)

    async def test_apply_empty_batch_is_noop(self, adapter):
        r = await adapter.apply(self.scope, Batch(scope=self.scope))
        assert isinstance(r, ApplyReport) and r.deleted == 0 and r.inserted == 0

    async def test_apply_rejects_foreign_scope_batch(self, adapter):
        with pytest.raises(ValueError):
            await adapter.apply(self.scope, Batch(scope="other"))

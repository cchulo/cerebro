"""Contract test harness. Every adapter test module subclasses the mixin for its contract and provides the
`adapter` fixture (usually against a mocked engine, e.g. with respx). The mixins check the contract's shape and the
invariants every implementation must keep; adapter-specific behaviour gets its own tests next to them.

    from tests.contracts.docs import DocumentIndexContract
    class TestLightRAG(DocumentIndexContract):
        @pytest.fixture
        def adapter(self, ctx): ...
"""

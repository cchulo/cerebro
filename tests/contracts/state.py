import pytest
from cerebro.core.contracts import SyncState


class SyncStateContract:
    @pytest.fixture
    def adapter(self, ctx) -> SyncState:
        raise NotImplementedError

    def test_roundtrip(self, adapter):
        assert isinstance(adapter, SyncState)
        assert adapter.get("confluence:public:ENG/1") is None
        adapter.commit({"confluence:public:ENG/1": "v3", "confluence:public:ENG/2": "v1", "git:public:x/README.md": "abc"})
        assert adapter.get("confluence:public:ENG/1") == "v3"
        assert sorted(adapter.keys_with_prefix("confluence:public:")) == ["confluence:public:ENG/1", "confluence:public:ENG/2"]
        adapter.commit({"confluence:public:ENG/1": "v4"}, delete_keys={"confluence:public:ENG/2"})
        assert adapter.get("confluence:public:ENG/1") == "v4" and adapter.get("confluence:public:ENG/2") is None
        assert adapter.keys_with_prefix("git:") == ["git:public:x/README.md"]

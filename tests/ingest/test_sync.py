"""The sync engine with the real `files` plugin over tests/fixtures/docs and a FakeIndex."""
import pytest
from cerebro.core import AdapterContext
from cerebro.core.contracts import Batch
from cerebro.core.contracts.sources import Source, Document
from cerebro.adapters.state.json_file import Adapter as JsonState
from cerebro.ingest import sync, Ingest
from cerebro.ingest.plugins import SourceRegistry
from tests.ingest.conftest import FakeIndex, make_config

PUBLIC = {"files:public:public/deploy.md", "files:public:public/onboarding.txt", "files:public:public/adr/0001.md"}


def test_first_sync_upserts_everything_and_commits_state(ingest, index):
    report = sync(ingest)
    assert report["public/files"] == {"changed": 3, "removed": 0} and report["infra/files"] == {"changed": 1, "removed": 0}
    assert report["public"]["index"] == {"scope": "public", "deleted": 3, "inserted": 3}      # upsert = delete + insert
    assert [b.scope for b in index.batches] == ["public", "infra"]
    assert set(index.all_upserts()) == PUBLIC | {"files:infra:infra/postgres.md"}
    assert index.all_upserts()["files:public:public/deploy.md"].endswith("We deploy with `make up`. The gateway listens on 8090.\n")
    assert next(d.title for d in index.batches[0].upserts if d.source_id.endswith("adr/0001.md")) == "adr/0001.md"
    assert sorted(ingest.state.keys_with_prefix("files:public:")) == sorted(PUBLIC)
    assert ingest.state.get("files:infra:infra/postgres.md")


def test_second_sync_only_sends_what_changed(ingest, index, docs_dir):
    sync(ingest)
    assert sync(ingest) == {"public/files": {"changed": 0, "removed": 0}, "infra/files": {"changed": 0, "removed": 0}}
    assert len(index.batches) == 2                                   # nothing reached the index
    (docs_dir / "public" / "deploy.md").write_text("# Deploying\n\nNow with helm.\n")
    report = sync(ingest)
    assert report["public/files"] == {"changed": 1, "removed": 0} and "infra" not in report
    b = index.batches[-1]
    assert [d.source_id for d in b.upserts] == ["files:public:public/deploy.md"] and b.deletes == {"files:public:public/deploy.md"}
    assert "helm" in b.upserts[0].text


def test_vanished_documents_are_deleted_and_forgotten(ingest, index, docs_dir):
    sync(ingest)
    (docs_dir / "public" / "onboarding.txt").unlink()
    report = sync(ingest)
    assert report["public/files"] == {"changed": 0, "removed": 1}
    b = index.batches[-1]
    assert b.deletes == {"files:public:public/onboarding.txt"} and b.upserts == []
    assert ingest.state.get("files:public:public/onboarding.txt") is None
    assert len(ingest.state.keys_with_prefix("files:public:")) == 2


def test_index_failure_leaves_state_untouched(ingest, index):
    index.fail = RuntimeError("docs-public: File already exists")
    with pytest.raises(RuntimeError, match="already exists"):
        sync(ingest)
    assert ingest.state.keys_with_prefix("") == []
    index.fail = None
    assert sync(ingest)["public/files"]["changed"] == 3                # the retry re-sends everything


def test_scope_and_source_selection(ingest, index):
    report = sync(ingest, only_scopes=["infra"])
    assert set(report) == {"infra/files", "infra"} and [b.scope for b in index.batches] == ["infra"]
    assert sync(ingest, source_name="nope") == {}                      # no scope lists it: nothing happens


# ------------------------------------------------------------------------------------------------ filtered (webhook) runs
class PrefixSource(Source):
    """A source whose webhook filter {"prefix": "a"} enumerates only keys under that prefix, like confluence's
    {"space": ...} or git's {"repo": ...}. ITEMS is mutated by the test to simulate the system of record."""
    name = "prefixed"
    ITEMS: dict[str, str] = {}

    def documents(self, ctx, filter=None):
        for key, text in sorted(self.ITEMS.items()):
            if not filter or key.startswith(filter["prefix"]):
                yield Document(key=key, version=str(hash(text)), text=text, title=key)

    def covers(self, key, filter):
        return not filter or key.startswith(filter["prefix"])


@pytest.fixture
def prefixed(docs_dir, tmp_path):
    cfg = make_config(docs_dir, sources={"prefixed": {"type": f"{__name__}:PrefixSource", "tone": "x"}})
    cfg.scopes["public"].docs = {"prefixed": {}}
    del cfg.scopes["infra"]
    PrefixSource.ITEMS = {"a/1": "one", "a/2": "two", "b/1": "bee"}
    ctx = AdapterContext(cfg)
    index = FakeIndex()
    return Ingest(cfg, index=index, state=JsonState({"dir": str(tmp_path / "s")}, ctx), sources=SourceRegistry(cfg), ctx=ctx), index


def test_filtered_run_deletes_only_what_the_filter_covers(prefixed):
    ingest, index = prefixed
    assert ingest.sources.load("prefixed").option("tone") == "x"        # options from sources:, `type` stripped
    sync(ingest)
    assert set(ingest.state.keys_with_prefix("prefixed:public:")) == {"prefixed:public:a/1", "prefixed:public:a/2", "prefixed:public:b/1"}
    PrefixSource.ITEMS = {"a/1": "one changed"}                        # a/2 and b/1 vanished from the system of record
    report = sync(ingest, "prefixed", {"prefix": "a"})
    assert report["public/prefixed"] == {"changed": 1, "removed": 1}
    b = index.batches[-1]
    assert b.deletes == {"prefixed:public:a/1", "prefixed:public:a/2"}   # b/1 is outside the filter: kept
    assert ingest.state.get("prefixed:public:b/1") is not None and ingest.state.get("prefixed:public:a/2") is None
    assert sync(ingest, "prefixed")["public/prefixed"] == {"changed": 0, "removed": 1}   # a full run reconciles b/1


def test_unconfigured_plugin_is_skipped_with_a_note(prefixed):
    ingest, index = prefixed
    PrefixSource.configured = lambda self: False
    try:
        assert sync(ingest) == {"public/prefixed": {"skipped": "prefixed not configured"}} and index.batches == []
    finally:
        del PrefixSource.configured


def test_unknown_plugin_raises(ingest):
    ingest.config.scopes["public"].docs["nothere"] = {}
    with pytest.raises(KeyError, match="no plugin with an ingest part named 'nothere'"):
        sync(ingest)


def test_batch_semantics():
    b = Batch(scope="s")
    assert b.empty
    b.upsert("k", "t"); b.delete("d")
    assert b.deletes == {"k", "d"} and [u.source_id for u in b.upserts] == ["k"] and not b.empty

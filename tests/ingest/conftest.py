"""Ingest fixtures: a FakeIndex that records batches, the `files` plugin over tests/fixtures/docs (copied to a
temp dir so tests can edit and delete files), json_file state in a temp dir."""
import pathlib, shutil
import pytest
from cerebro.core import Health, AdapterContext, StaticLocator
from cerebro.core.config import Config
from cerebro.core.contracts import DocumentIndex, Batch, ApplyReport, DocAnswer, QueryOptions
from cerebro.adapters.state.json_file import Adapter as JsonState
from cerebro.ingest.plugins import SourceRegistry
from cerebro.ingest.runtime import Ingest

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "docs"


class FakeIndex(DocumentIndex):
    """Records every batch; `fail` makes apply raise (the engine must then leave state untouched)."""
    name = "fake"
    modes = ("default", "fast")
    default_mode = "default"

    def __init__(self, options=None, ctx=None):
        super().__init__(options, ctx)
        self.batches: list[Batch] = []
        self.fail: Exception | None = None
        self.down: set[str] = set()

    async def apply(self, scope, batch):
        if batch.scope != scope:
            raise ValueError("foreign batch")
        if self.fail:
            raise self.fail
        self.batches.append(batch.model_copy(deep=True))
        return ApplyReport(scope=scope, deleted=len(batch.deletes), inserted=len(batch.upserts))

    async def query(self, scope, query, opts=None):
        return DocAnswer(scope=scope, answer="", answered=False)

    async def health(self, scope):
        return Health.down("unit stopped") if scope in self.down else Health.up(documents=len(self.batches))

    def all_upserts(self) -> dict[str, str]:
        return {d.source_id: d.text for b in self.batches for d in b.upserts}

    def all_deletes(self) -> set[str]:
        return {k for b in self.batches for k in b.deletes}


class Secrets:
    def __init__(self, **values): self.values = dict(values)
    def get(self, name, default=None): return self.values.get(name, default)


@pytest.fixture
def docs_dir(tmp_path):
    d = tmp_path / "docs"
    shutil.copytree(FIXTURES, d)
    return d


def make_config(docs_dir: pathlib.Path, **extra) -> Config:
    return Config.model_validate({
        "gateway": {"plugins_dir": str(ROOT / "plugins")},
        "engines": {"docs": {"type": "tests.ingest.conftest:FakeIndex"}},
        "scopes": {
            "public": {"groups": ["everyone"], "docs": {"files": {"paths": [str(docs_dir / "public")]}}},
            "infra": {"groups": ["sre"], "docs": {"files": {"paths": [str(docs_dir / "infra")]}}},
        },
        **extra,
    })


@pytest.fixture
def config(docs_dir):
    return make_config(docs_dir)


@pytest.fixture
def index():
    return FakeIndex()


@pytest.fixture
def ingest(config, index, tmp_path):
    ctx = AdapterContext(config, secrets=Secrets(INGEST_WEBHOOK_SECRET="s3"), locator=StaticLocator())
    return Ingest(config, index=index, state=JsonState({"dir": str(tmp_path / "state")}, ctx),
                  sources=SourceRegistry(config), ctx=ctx)

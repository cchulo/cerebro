"""pgvector docs adapter. Chunking, DSN and answer shaping are pure; the contract run and everything that touches
the table need a live Postgres with the vector extension: set CEREBRO_TEST_PG_DSN, e.g.

    docker run -d --rm -e POSTGRES_PASSWORD=x -p 55432:5432 pgvector/pgvector:pg16
    CEREBRO_TEST_PG_DSN="host=localhost port=55432 user=postgres password=x dbname=postgres" pytest tests/adapters/docs

Embeddings come from a fake Inference adapter: a deterministic bag-of-words hash, so "deploy" lands near a chunk
that talks about deploying and far from one about coffee, with no model involved."""
import hashlib, math, os
import pytest
from cerebro.core import Health
from cerebro.core.contracts import Batch, QueryOptions
from cerebro.core.contracts.inference import Inference, ChatMessage
from cerebro.adapters.docs.pgvector import Adapter, TABLE, chunk_text, header
from tests.contracts.docs import DocumentIndexContract

DSN = os.environ.get("CEREBRO_TEST_PG_DSN")
DIM = 16
needs_pg = pytest.mark.skipif(not DSN, reason="CEREBRO_TEST_PG_DSN not set")


class FakeInference(Inference):
    """Deterministic embeddings: words hashed into DIM buckets, L2-normalised. Records every call."""
    name = "fake"

    def __init__(self, options=None, ctx=None):
        super().__init__(options, ctx)
        self.calls: list[list[str]] = []
        self.closed = False

    async def chat(self, messages: list[ChatMessage], *, model=None, temperature=0.0, max_tokens=None) -> str:
        return ""

    async def embed(self, texts: list[str], *, model=None) -> list[list[float]]:
        self.calls.append(list(texts))
        out = []
        for t in texts:
            v = [0.0] * DIM
            for w in t.lower().split():
                w = w.strip(".,;:()#!?\"'")
                if w:
                    v[int(hashlib.sha1(w.encode()).hexdigest(), 16) % DIM] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out

    async def aclose(self) -> None:
        self.closed = True

    async def health(self) -> Health:
        return Health.up("fake")


# ------------------------------------------------------------------------------------------------ pure parts
def test_chunking_respects_paragraphs_size_and_overlap():
    paras = [f"Paragraph {i}. " + "word " * 60 for i in range(8)]          # 312 chars each once stripped
    text = "\n\n".join(paras)
    chunks = chunk_text(text, size=1000, overlap=150)
    # 3 paragraphs fit the first chunk; later chunks also carry 150 chars of overlap, so 2 fit: 3 + 2 + 2 + 1
    assert len(chunks) == 4 and all(len(c) <= 1000 for c in chunks)
    assert chunks[0].startswith("Paragraph 0.") and "Paragraph 2." in chunks[0] and "Paragraph 3." not in chunks[0]
    assert "Paragraph 3." in chunks[1] and "Paragraph 4." in chunks[1]     # paragraphs never split when they fit
    assert chunks[1].startswith("word word") and len(chunks[1].split("\n\n")[0]) <= 150   # overlap from chunk 0
    assert chunks[3].endswith("Paragraph 7. " + "word " * 59 + "word")
    assert chunk_text("") == [] and chunk_text("   \n\n  ") == []
    assert chunk_text("short") == ["short"]


def test_long_paragraph_is_cut_at_sentences_then_words():
    sentences = " ".join(f"Sentence number {i} says something useful." for i in range(40))   # one paragraph, ~1700 chars
    chunks = chunk_text(sentences, size=400, overlap=0)
    assert len(chunks) >= 5 and all(len(c) <= 400 for c in chunks)
    assert all(c.endswith(".") for c in chunks)                              # cut at sentence ends
    unbroken = "x" * 2500                                                    # nothing to cut at: hard split
    assert [len(c) for c in chunk_text(unbroken, size=1000, overlap=0)] == [1000, 1000, 500]
    words = "w " * 1200
    assert all(len(c) <= 100 for c in chunk_text(words, size=100, overlap=0))


def test_header_carries_title_and_source():
    assert header("x/README.md", "git:public:x/README.md") == "# x/README.md\n(git:public:x/README.md)"
    assert header("", "git:public:x/README.md") == "# git:public:x/README.md"


def test_dsn_from_options_env_and_secret(ctx, monkeypatch):
    assert Adapter({"dsn": "postgresql://u:p@h/db"}, ctx).dsn() == "postgresql://u:p@h/db"
    for k in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_DATABASE", "POSTGRES_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    assert Adapter({}, ctx).dsn() == "host=postgres port=5432 user=cerebro dbname=cerebro"
    assert Adapter({}, ctx).configured() is False
    ctx.secrets.values["POSTGRES_PASSWORD"] = "s3cret"
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    a = Adapter({"database": "other"}, ctx)
    assert a.dsn() == "host=db.internal port=5432 user=cerebro dbname=other password=s3cret" and a.configured()


def test_no_units_and_dim_from_config(ctx):
    a = Adapter({}, ctx)
    assert a.units() == [] and a.jobs() == [] and a.modes == ("vector",) and a.default_mode == "vector"
    assert a.dim == 1024 and Adapter({"dim": 16}, ctx).dim == 16      # example config: bge-m3, dim 1024


def test_plan_provisions_nothing_for_the_docs_engine(ctx):
    """engines.docs: {type: pgvector} adds no unit: the data lives in the shared postgres the plan creates anyway."""
    from cerebro.provision.plan import plan
    units, jobs = plan(ctx.config, ctx, adapters=[Adapter({}, ctx)])
    by = {u.name: u for u in units}
    assert not [u for u in units if u.role == "docs"] and jobs == []
    assert by["postgres"].image.startswith("pgvector/pgvector:") and "CREATE DATABASE cerebro;" in by["postgres"].files["/docker-entrypoint-initdb.d/init.sql"]
    assert by["ingest"].depends_on == ["postgres"] and "postgres" in by["gateway"].depends_on


async def test_health_down_when_unreachable(ctx):
    h = await Adapter({"dsn": "host=127.0.0.1 port=1 user=x dbname=x connect_timeout=1", "dim": DIM}, ctx).health("public")
    assert isinstance(h, Health) and not h.ok and "pgvector (public)" in h.detail


# ------------------------------------------------------------------------------------------------ live postgres
@pytest.fixture
def fake_inference():
    return FakeInference()


@pytest.fixture
def adapter(ctx, fake_inference):
    """A fresh table per test: the fixture drops it before and after."""
    ctx.inference = fake_inference
    a = Adapter({"dsn": DSN, "dim": DIM, "chunk_size": 200, "chunk_overlap": 40}, ctx)
    a._run_sync(lambda conn: conn.execute(f"DROP TABLE IF EXISTS {TABLE}"))
    a._ready = False
    yield a
    a._run_sync(lambda conn: conn.execute(f"DROP TABLE IF EXISTS {TABLE}"))


def rows(adapter, scope=None):
    def q(conn):
        if scope is None:
            return conn.execute(f"SELECT scope, source_id, chunk_no, text FROM {TABLE} ORDER BY 1, 2, 3").fetchall()
        return conn.execute(f"SELECT scope, source_id, chunk_no, text FROM {TABLE} WHERE scope = %s ORDER BY 1, 2, 3", (scope,)).fetchall()
    return adapter._run_sync(q)


# 3 paragraphs, 228 chars: two chunks at the fixture's chunk_size of 200 (the first two paragraphs, then the third)
DEPLOY = ("Deploy the gateway with helm upgrade. Deploy runs the migrations first.\n\nThe deploy job needs the kube token."
          "\n\nRollback: run helm rollback gateway to the previous revision when the readiness probe keeps failing after ten minutes.")
COFFEE = "The office coffee machine wants descaling every month. Coffee beans are in the second drawer."


@needs_pg
class TestPgvectorContract(DocumentIndexContract):
    scope = "public"

    @pytest.fixture
    def adapter(self, adapter):
        return adapter


@needs_pg
async def test_schema_created_on_first_use_and_health(adapter):
    h = await adapter.health("public")
    assert h.ok and h.data["table"] is True and h.data["vector"] and h.data["dim"] == DIM
    idx = adapter._run_sync(lambda c: c.execute(
        "SELECT indexdef FROM pg_indexes WHERE tablename = %s AND indexdef LIKE '%%hnsw%%'", (TABLE,)).fetchall())
    assert len(idx) == 1 and "vector_cosine_ops" in idx[0][0]
    assert await adapter.stats("public") == {"sources": 0, "chunks": 0, "dim": DIM}


@needs_pg
async def test_apply_chunks_embeds_with_header_and_inserts(adapter, fake_inference):
    b = Batch(scope="public")
    b.upsert("git:public:x/README.md", DEPLOY, title="x/README.md")
    b.upsert("files:public:coffee.md", COFFEE)
    r = await adapter.apply("public", b)
    assert (r.deleted, r.inserted) == (0, 2) and r.note == "3 chunks embedded"
    assert [(sc, s, n) for sc, s, n, _ in rows(adapter)] == [("public", "files:public:coffee.md", 0),
                                                            ("public", "git:public:x/README.md", 0), ("public", "git:public:x/README.md", 1)]
    embedded = [t for call in fake_inference.calls for t in call]
    assert embedded[0].startswith("# x/README.md\n(git:public:x/README.md)\n\nDeploy the gateway")
    assert embedded[2].startswith("# files:public:coffee.md\n\nThe office coffee")
    assert await adapter.stats("public") == {"sources": 2, "chunks": 3, "dim": DIM}


@needs_pg
async def test_query_returns_passages_grouped_by_source_with_references(adapter):
    b = Batch(scope="public")
    b.upsert("git:public:x/README.md", DEPLOY, title="x/README.md")
    b.upsert("files:public:coffee.md", COFFEE)
    await adapter.apply("public", b)
    a = await adapter.query("public", "how do we deploy the gateway?", QueryOptions(top_k=5))
    assert a.scope == "public" and a.answered is True
    assert a.answer.startswith("## x/README.md\nsource: git:public:x/README.md\n\nDeploy the gateway")
    assert "[...]" in a.answer and a.answer.index("git:public:x/README.md") < a.answer.index("files:public:coffee.md")
    assert [(r.id, r.source, r.title) for r in a.references] == [("1", "git:public:x/README.md", "x/README.md"),
                                                                 ("2", "files:public:coffee.md", None)]
    assert a.references[0].excerpt.startswith("Deploy the gateway")
    assert a.raw["hits"][0]["source_id"] == "git:public:x/README.md" and a.raw["max_distance"] == 0.6
    assert len(a.raw["hits"]) == 3 and a.raw["hits"] == sorted(a.raw["hits"], key=lambda h: h["distance"])
    assert len((await adapter.query("public", "deploy", QueryOptions(top_k=1))).raw["hits"]) == 1
    with pytest.raises(ValueError):
        await adapter.query("public", "q", QueryOptions(mode="hybrid"))


@needs_pg
async def test_answered_threshold_is_an_option(adapter):
    b = Batch(scope="public"); b.upsert("files:public:coffee.md", COFFEE)
    await adapter.apply("public", b)
    miss = await adapter.query("public", "kubernetes ingress certificates")
    assert miss.answered is False and len(miss.references) == 1        # passages still returned, just not an answer
    assert all(h["distance"] >= 0.6 for h in miss.raw["hits"])
    hit = await adapter.query("public", "descaling the coffee machine")
    assert hit.answered is True
    assert (await adapter.query("public", "descaling the coffee machine", QueryOptions(extra={"max_distance": 0.0}))).answered is False
    adapter.options["max_distance"] = 1.5
    assert (await adapter.query("public", "kubernetes ingress certificates")).answered is True
    assert (await adapter.query("public", "anything")).raw["max_distance"] == 1.5
    assert (await adapter.query("infra", "anything")).answered is False   # empty scope: nothing to answer with


@needs_pg
async def test_scopes_never_cross(adapter):
    """Same source id in two scopes with different content; each scope sees only its own rows."""
    pub = Batch(scope="public"); pub.upsert("git:shared:README.md", DEPLOY, title="deploy")
    pay = Batch(scope="payments"); pay.upsert("git:shared:README.md", COFFEE, title="coffee")
    await adapter.apply("public", pub); await adapter.apply("payments", pay)
    assert {(s, sid) for s, sid, _, _ in rows(adapter)} == {("public", "git:shared:README.md"), ("payments", "git:shared:README.md")}
    a = await adapter.query("payments", "how do we deploy the gateway?", QueryOptions(top_k=10))
    assert a.answered is False and "coffee" in a.answer and "Deploy" not in a.answer
    assert [r.source for r in a.references] == ["git:shared:README.md"] and a.references[0].title == "coffee"
    a = await adapter.query("public", "coffee beans", QueryOptions(top_k=10))
    assert "Coffee" not in a.answer and a.references[0].title == "deploy"
    assert await adapter.stats("public") == {"sources": 1, "chunks": 2, "dim": DIM}
    assert await adapter.stats("payments") == {"sources": 1, "chunks": 1, "dim": DIM}
    # a delete in one scope leaves the other scope's rows with the same id alone
    d = Batch(scope="public"); d.delete("git:shared:README.md")
    assert (await adapter.apply("public", d)).deleted == 1
    assert [(s, sid) for s, sid, _, _ in rows(adapter)] == [("payments", "git:shared:README.md")]
    with pytest.raises(ValueError):
        await adapter.apply("public", pay)


@needs_pg
async def test_upsert_replaces_old_chunks_and_delete_removes_them(adapter):
    b = Batch(scope="public"); b.upsert("git:public:doc.md", DEPLOY, title="v1")
    b.upsert("git:public:other.md", COFFEE)
    await adapter.apply("public", b)
    assert len(rows(adapter, "public")) == 3
    b = Batch(scope="public"); b.upsert("git:public:doc.md", "One short paragraph now.", title="v2")
    r = await adapter.apply("public", b)
    assert (r.deleted, r.inserted) == (1, 1)
    assert [(sid, n, t) for _, sid, n, t in rows(adapter, "public")] == [
        ("git:public:doc.md", 0, "One short paragraph now."), ("git:public:other.md", 0, COFFEE)]
    assert (await adapter.query("public", "helm upgrade migrations")).references[0].title != "v1"
    b = Batch(scope="public"); b.delete("git:public:doc.md"); b.delete("git:public:never-there.md")
    r = await adapter.apply("public", b)
    assert (r.deleted, r.inserted) == (1, 0) and r.note is None
    assert [sid for _, sid, _, _ in rows(adapter, "public")] == ["git:public:other.md"]
    assert await adapter.stats("public") == {"sources": 1, "chunks": 1, "dim": DIM}


@needs_pg
async def test_apply_is_atomic_when_embedding_fails(adapter, fake_inference):
    b = Batch(scope="public"); b.upsert("git:public:doc.md", DEPLOY)
    await adapter.apply("public", b)

    async def boom(texts, *, model=None):
        raise RuntimeError("embedding endpoint down")
    fake_inference.embed = boom
    b = Batch(scope="public"); b.upsert("git:public:doc.md", "new"); b.upsert("git:public:new.md", "x")
    with pytest.raises(RuntimeError, match="endpoint down"):
        await adapter.apply("public", b)
    assert [(sid, n) for _, sid, n, _ in rows(adapter, "public")] == [("git:public:doc.md", 0), ("git:public:doc.md", 1)]


@needs_pg
async def test_wrong_dimension_is_refused(adapter, fake_inference):
    async def wrong(texts, *, model=None):
        return [[0.1] * (DIM + 1) for _ in texts]
    fake_inference.embed = wrong
    b = Batch(scope="public"); b.upsert("git:public:doc.md", "x")
    with pytest.raises(RuntimeError, match="dimensions"):
        await adapter.apply("public", b)


@needs_pg
async def test_builds_its_own_inference_adapter_when_ctx_has_none(ctx):
    """ctx.inference is None -> registry.build(config.inference.type) per operation, closed afterwards."""
    cfg = ctx.config.model_copy(deep=True)
    cfg.inference.type = "tests.adapters.docs.test_pgvector:FakeInference"
    ctx.config, ctx.inference = cfg, None
    a = Adapter({"dsn": DSN, "dim": DIM}, ctx)
    a._run_sync(lambda conn: conn.execute(f"DROP TABLE IF EXISTS {TABLE}")); a._ready = False
    try:
        b = Batch(scope="public"); b.upsert("files:public:coffee.md", COFFEE)
        assert (await a.apply("public", b)).inserted == 1
        assert (await a.query("public", "coffee beans")).answered is True
    finally:
        a._run_sync(lambda conn: conn.execute(f"DROP TABLE IF EXISTS {TABLE}"))


@needs_pg
def test_apply_from_a_worker_thread_with_its_own_loop(adapter):
    """The ingest engine calls apply() through run_async from sync code, possibly inside another loop."""
    from cerebro.ingest.runtime import run_async
    b = Batch(scope="public"); b.upsert("git:public:doc.md", DEPLOY)
    assert run_async(adapter.apply("public", b)).inserted == 1
    assert run_async(adapter.query("public", "deploy the gateway")).answered is True

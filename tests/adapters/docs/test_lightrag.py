"""LightRAG adapter against a fake of the v1.5.7 server API (respx). Nothing here talks to a real LightRAG; the
fake encodes the verified facts: basename-only file_source ids, 409 on re-insert, busy deletes, paginated listing."""
import json
import httpx, pytest, respx
from cerebro.core import Health
from cerebro.core.contracts import Batch, QueryOptions, DocumentText
from cerebro.adapters.docs.lightrag import Adapter, encode_source, decode_source, answered, workspace
from tests.contracts.docs import DocumentIndexContract

BASE = "http://docs-public:8080"          # conftest's StaticLocator: http://{unit}:8080, unit docs-public


class FakeLightRAG:
    """Just enough of the LightRAG 1.5.7 document API to drive the adapter: documents keyed by encoded file_path."""
    def __init__(self, router: respx.Router, base: str = BASE, *, busy_deletes: int = 0, busy_polls: int = 0):
        self.docs: dict[str, dict] = {}          # doc id -> {file_path, status}
        self.inserted: list[dict] = []           # every /documents/texts payload
        self.deleted: list[list[str]] = []
        self.busy_deletes = busy_deletes         # "busy" answers before a delete is accepted
        self.busy_polls = busy_polls             # pipeline_status answers with busy=True before idle
        self.query_body: dict = {"response": "", "references": [], "llm_generated": True}
        self.queries: list[dict] = []
        self._n = 0
        r = router
        r.get(f"{base}/health").mock(return_value=httpx.Response(200, json={"status": "healthy", "core_version": "1.5.7"}))
        r.get(f"{base}/documents/pipeline_status").mock(side_effect=self._status)
        r.post(f"{base}/documents/paginated").mock(side_effect=self._paginated)
        r.delete(f"{base}/documents/delete_document").mock(side_effect=self._delete)
        r.post(f"{base}/documents/texts").mock(side_effect=self._texts)
        r.post(f"{base}/query").mock(side_effect=self._query)

    def seed(self, source_id: str, status: str = "PROCESSED") -> str:
        self._n += 1
        did = f"doc-{self._n}"
        self.docs[did] = {"file_path": encode_source(source_id), "status": status}
        return did

    def _status(self, request):
        busy = self.busy_polls > 0
        self.busy_polls -= 1
        return httpx.Response(200, json={"busy": busy, "scanning": False, "pending_enqueues": False,
                                         "destructive_busy": False, "latest_message": "processing" if busy else "idle"})

    def _paginated(self, request):
        body = json.loads(request.content)
        page, size = body["page"], body["page_size"]
        items = [{"id": i, **d} for i, d in self.docs.items()]
        chunk = items[(page - 1) * size: page * size]
        counts: dict[str, int] = {}
        for d in self.docs.values():
            counts[d["status"]] = counts.get(d["status"], 0) + 1
        return httpx.Response(200, json={"documents": chunk, "status_counts": counts,
                                         "pagination": {"page": page, "has_next": page * size < len(items)}})

    def _delete(self, request):
        body = json.loads(request.content)
        if self.busy_deletes > 0:
            self.busy_deletes -= 1
            return httpx.Response(200, json={"status": "busy", "message": "pipeline busy"})
        for did in body["doc_ids"]:
            self.docs.pop(did, None)
        self.deleted.append(list(body["doc_ids"]))
        return httpx.Response(200, json={"status": "deletion_started"})

    def _texts(self, request):
        body = json.loads(request.content)
        existing = {d["file_path"] for d in self.docs.values()}
        for fs in body["file_sources"]:
            if "/" in fs:
                return httpx.Response(400, json={"detail": "file_source contains '/': basename would collide"})
            if fs in existing:
                return httpx.Response(409, json={"detail": f"File '{fs}' already exists in the input directory"})
        self.inserted.append(body)
        for fs in body["file_sources"]:
            self._n += 1
            self.docs[f"doc-{self._n}"] = {"file_path": fs, "status": "PENDING"}
        return httpx.Response(200, json={"status": "success", "message": f"{len(body['texts'])} texts enqueued"})

    def _query(self, request):
        self.queries.append(json.loads(request.content))
        return httpx.Response(200, json=self.query_body)


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as r:
        yield r


@pytest.fixture
def fake(router):
    return FakeLightRAG(router)


@pytest.fixture
def adapter(ctx, fake):
    ctx.secrets.values["LIGHTRAG_API_KEY"] = "k-test"
    return Adapter({"poll_interval": 0, "insert_chunk": 25, "max_async": 4, "think": True}, ctx)


class TestLightRAGContract(DocumentIndexContract):
    scope = "public"

    @pytest.fixture
    def adapter(self, adapter):
        return adapter


# ------------------------------------------------------------------------------------------------ id encoding
def test_source_ids_never_contain_slashes():
    sid = "confluence:public:ENG/123"
    assert "/" not in encode_source(sid) and decode_source(encode_source(sid)) == sid
    assert workspace("pay-ments") == "scope_pay_ments"


# ------------------------------------------------------------------------------------------------ apply
async def test_apply_deletes_previous_versions_then_inserts(adapter, fake):
    old = fake.seed("git:public:x/README.md")
    fake.seed("git:public:x/docs/other.md")                       # untouched
    b = Batch(scope="public")
    b.upsert("git:public:x/README.md", "hello", title="x/README.md")
    b.upsert("git:public:x/new.md", "fresh")
    b.delete("git:public:x/gone.md")                              # not in the index: no delete call for it
    r = await adapter.apply("public", b)
    assert (r.deleted, r.inserted) == (1, 2)
    assert fake.deleted == [[old]]
    payload = fake.inserted[0]
    assert payload["file_sources"] == ["git:public:x|README.md", "git:public:x|new.md"]
    assert payload["texts"][0].startswith("# x/README.md\n\nhello") and payload["texts"][1] == "fresh"
    assert {d["file_path"] for d in fake.docs.values()} == {"git:public:x|README.md", "git:public:x|new.md",
                                                            "git:public:x|docs|other.md"}


async def test_apply_sends_api_key_and_chunks_inserts(adapter, fake, router):
    adapter.options["insert_chunk"] = 2
    b = Batch(scope="public")
    for i in range(5):
        b.upsert(f"files:public:d/{i}.md", f"t{i}")
    r = await adapter.apply("public", b)
    assert r.inserted == 5 and [len(p["texts"]) for p in fake.inserted] == [2, 2, 1]
    assert all(c.request.headers["x-api-key"] == "k-test" for c in router.calls)


async def test_apply_retries_while_delete_is_busy(ctx, router):
    fake = FakeLightRAG(router, busy_deletes=2, busy_polls=1)
    fake.seed("files:public:a.md")
    a = Adapter({"poll_interval": 0}, ctx)
    b = Batch(scope="public"); b.delete("files:public:a.md")
    r = await a.apply("public", b)
    assert (r.deleted, r.inserted) == (1, 0) and len(fake.deleted) == 1 and not fake.docs
    assert len([c for c in router.calls if c.request.method == "DELETE"]) == 3


async def test_apply_gives_up_when_pipeline_never_idles(ctx, router):
    fake = FakeLightRAG(router, busy_polls=10 ** 6)
    fake.seed("files:public:a.md")
    a = Adapter({"poll_interval": 0, "idle_timeout": 0}, ctx)
    b = Batch(scope="public"); b.delete("files:public:a.md")
    with pytest.raises(TimeoutError, match="docs-public pipeline still busy"):
        await a.apply("public", b)


async def test_apply_surfaces_409_before_state_is_committed(adapter, fake):
    fake.seed("files:public:a.md")
    b = Batch(scope="public")
    b.upserts.append(DocumentText(source_id="files:public:a.md", text="dup"))   # upsert() without the delete
    with pytest.raises(RuntimeError, match="already exists"):
        await adapter.apply("public", b)


async def test_apply_paginates_the_listing(adapter, fake, router):
    for i in range(450):
        fake.seed(f"files:public:{i}.md")
    b = Batch(scope="public"); b.delete("files:public:449.md")
    r = await adapter.apply("public", b)
    assert r.deleted == 1
    assert len([c for c in router.calls if c.request.url.path == "/documents/paginated"]) == 3


async def test_apply_uses_the_scope_unit_endpoint(ctx, router):
    other = FakeLightRAG(router, base="http://docs-infra:8080")
    a = Adapter({"poll_interval": 0}, ctx)
    b = Batch(scope="infra"); b.upsert("files:infra:r.md", "x")
    assert (await a.apply("infra", b)).inserted == 1 and len(other.inserted) == 1


# ------------------------------------------------------------------------------------------------ query
async def test_query_decodes_references_and_answer(adapter, fake):
    fake.query_body = {"response": "See [1] git:public:x|docs|deploy.md for the steps.", "llm_generated": True,
                       "references": [{"reference_id": "1", "file_path": "git:public:x|docs|deploy.md"}]}
    a = await adapter.query("public", "how do we deploy?", QueryOptions(mode="hybrid", extra={"top_k": 5}))
    assert a.answered and a.answer == "See [1] git:public:x/docs/deploy.md for the steps."
    assert [(r.id, r.source) for r in a.references] == [("1", "git:public:x/docs/deploy.md")]
    assert fake.queries[-1] == {"query": "how do we deploy?", "mode": "hybrid", "include_references": True, "top_k": 5}


async def test_query_default_mode_is_mix_and_rejects_unknown(adapter, fake):
    await adapter.query("public", "q")
    assert fake.queries[-1]["mode"] == "mix"
    with pytest.raises(ValueError):
        await adapter.query("public", "q", QueryOptions(mode="graphrag"))


@pytest.mark.parametrize("body,expect", [
    ({"response": "The gateway listens on 8090.", "references": [{"file_path": "a"}], "llm_generated": True}, True),
    ({"response": "The gateway listens on 8090.", "references": [], "llm_generated": True}, False),
    ({"response": "[no-context] Sorry", "references": [{"file_path": "a"}], "llm_generated": False}, False),
    ({"response": "The documents do not contain any information about Kafka.", "references": [{"file_path": "a"}]}, False),
    ({"response": "There is no specific mention of Kafka, but Redis is used.", "references": [{"file_path": "a"}]}, False),
    ({"response": "I could not find that in the provided context.", "references": [{"file_path": "a"}]}, False),
])
def test_answered_heuristic(body, expect):
    assert answered(body) is expect


async def test_query_not_answered_when_index_has_nothing(adapter, fake):
    fake.query_body = {"response": "Sorry, I'm not able to provide an answer to that question.[no-context]",
                       "references": [], "llm_generated": False}
    a = await adapter.query("public", "what about kafka?")
    assert a.answered is False and a.references == [] and a.raw["llm_generated"] is False


# ------------------------------------------------------------------------------------------------ health / stats
async def test_health_and_stats(adapter, fake):
    h = await adapter.health("public")
    assert isinstance(h, Health) and h.ok and h.data["core_version"] == "1.5.7"
    fake.seed("a", "PROCESSED"); fake.seed("b", "PROCESSED"); fake.seed("c", "FAILED")
    s = await adapter.stats("public")
    assert s["documents"] == {"PROCESSED": 2, "FAILED": 1} and s["total"] == 3 and s["busy"] is False


async def test_health_down_when_unreachable(adapter, router):
    router.get("http://docs-payments:8080/health").mock(side_effect=httpx.ConnectError("refused"))
    h = await adapter.health("payments")
    assert not h.ok and "ConnectError" in h.detail


# ------------------------------------------------------------------------------------------------ units
def test_units_one_lightrag_per_scope(adapter, example_config):
    units = {u.name: u for u in adapter.units()}
    assert set(units) == {"docs-public", "docs-payments", "docs-infra"}
    u = units["docs-public"]
    assert u.role == "docs" and u.image == "ghcr.io/hkuds/lightrag:v1.5.7" and u.http_port == 9621
    assert u.scope == "public" and u.depends_on == ["postgres"] and u.health_path == "/health"
    assert u.secret_env == ["LIGHTRAG_API_KEY", "POSTGRES_PASSWORD"]
    assert [(v.name, v.mount_path) for v in u.volumes] == [("data", "/app/data")]
    e = u.env
    assert e["WORKSPACE"] == "scope_public" and e["PORT"] == "9621"
    assert (e["POSTGRES_HOST"], e["POSTGRES_PORT"], e["POSTGRES_USER"], e["POSTGRES_DATABASE"]) == ("postgres", "5432", "cerebro", "lightrag")
    assert e["LIGHTRAG_KV_STORAGE"] == "PGKVStorage" and e["LIGHTRAG_GRAPH_STORAGE"] == "PGTableGraphStorage"
    assert e["LLM_BINDING"] == "ollama" and e["LLM_BINDING_HOST"] == "http://ollama:11434" and e["LLM_MODEL"] == "gpt-oss:20b"
    assert e["EMBEDDING_BINDING"] == "ollama" and e["EMBEDDING_MODEL"] == "bge-m3" and e["EMBEDDING_DIM"] == "1024"
    assert e["MAX_ASYNC"] == "4" and e["OLLAMA_LLM_THINK"] == "true" and e["MAX_GLEANING"] == "0"
    assert e["MAX_PARALLEL_INSERT"] == "1" and e["OLLAMA_LLM_NUM_PREDICT"] == "4096" and e["ENABLE_LLM_CACHE"] == "false"
    assert "POSTGRES_PASSWORD" not in e and "LIGHTRAG_API_KEY" not in e       # secrets never appear as values
    assert units["docs-payments"].env["WORKSPACE"] == "scope_payments"


def test_units_openai_provider_uses_secret_for_api_key(example_config, ctx):
    cfg = example_config.model_copy(deep=True)
    cfg.inference.llm.provider = "openai"; cfg.inference.llm.base_url = "https://llm.internal/v1"
    cfg.inference.llm.api_key_env = "LLM_API_KEY"
    cfg.engines.docs.idle_ttl = "1h"; cfg.engines.docs.resources = {"memory": "2Gi", "storage": "20Gi"}
    ctx.config = cfg
    u = Adapter({}, ctx).units()[0]
    assert u.env["LLM_BINDING"] == "openai" and u.env["LLM_BINDING_API_KEY"] == "${LLM_API_KEY}"
    assert u.env["EMBEDDING_BINDING_API_KEY"] == "ollama"
    assert u.secret_env == ["LIGHTRAG_API_KEY", "POSTGRES_PASSWORD", "LLM_API_KEY"]
    assert u.idle_ttl == "1h" and u.resources == {"memory": "2Gi"} and u.volumes[0].size == "20Gi"

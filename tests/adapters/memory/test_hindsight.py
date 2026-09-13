"""Hindsight adapter against a respx-mocked engine. Nothing here talks to a real Hindsight; the wire shapes are the
ones v1 used against 0.9.2 (see the module docstring of the adapter)."""
import httpx, pytest, respx
from cerebro.core import registry
from cerebro.core.config import ModelEndpoint, EmbedEndpoint
from cerebro.adapters.memory import hindsight
from tests.contracts.memory import MemoryStoreContract

BASE = "http://memory:8080"
BANK = "user-alice"

RECALL_BODY = {
    "results": [
        {"id": "m1", "text": "deployed checkout 2.3.1", "type": "world", "context": "deploy",
         "tags": ["deploy"], "mentioned_at": "2026-09-01T10:00:00Z", "scores": {"final": 0.83, "semantic": 0.7}},
        {"id": "m2", "text": "rollback flag in the runbook was wrong", "tags": None, "scores": None},
    ],
    "trace": {"budget": "mid"},
}


@pytest.fixture
def adapter(ctx):
    ctx.secrets.values["HINDSIGHT_API_KEY"] = "hs-secret"
    a = registry.build("memory", "hindsight", {"reranker": "rrf"}, ctx)
    yield a


@pytest.fixture
def api():
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post(f"/v1/default/banks/{BANK}/memories/recall").mock(return_value=httpx.Response(200, json=RECALL_BODY))
        mock.post(f"/v1/default/banks/{BANK}/memories").mock(
            return_value=httpx.Response(200, json={"operation_id": "op-1", "status": "pending"}))
        mock.post(f"/v1/default/banks/{BANK}/reflect").mock(
            return_value=httpx.Response(200, json={"text": "## What we know\n- 2.3.1 is out", "based_on": {}}))
        mock.get("/health").mock(return_value=httpx.Response(200, json={"status": "ok", "version": "0.9.2"}))
        yield mock


class TestHindsightContract(MemoryStoreContract):
    bank = BANK

    @pytest.fixture
    def adapter(self, adapter, api):
        return adapter


async def test_recall_maps_items_and_keeps_raw(adapter, api):
    r = await adapter.recall(BANK, "checkout deploys", budget="high", max_tokens=512)
    req = api.calls.last.request
    assert req.headers["authorization"] == "Bearer hs-secret"
    assert req.url.path == f"/v1/default/banks/{BANK}/memories/recall"
    assert httpx.Response(200, content=req.content).json() == {"query": "checkout deploys", "budget": "high", "max_tokens": 512}
    assert [m.id for m in r.results] == ["m1", "m2"]
    m1 = r.results[0]
    assert m1.content == "deployed checkout 2.3.1" and m1.context == "deploy" and m1.tags == ["deploy"]
    assert m1.created_at == "2026-09-01T10:00:00Z" and m1.score == 0.83
    assert r.results[1].tags == [] and r.results[1].score is None
    assert r.raw == RECALL_BODY and r.note is None


async def test_recall_rejects_bad_budget(adapter, api):
    with pytest.raises(ValueError):
        await adapter.recall(BANK, "x", budget="huge")
    assert not api.calls


async def test_empty_bank_is_not_an_error(adapter):
    with respx.mock(base_url=BASE) as mock:
        mock.post(f"/v1/default/banks/fresh/memories/recall").mock(return_value=httpx.Response(404, json={"detail": "not found"}))
        mock.post(f"/v1/default/banks/fresh/reflect").mock(return_value=httpx.Response(404))
        r = await adapter.recall("fresh", "anything")
        assert r.results == [] and r.note == "bank is empty" and r.bank == "fresh"
        x = await adapter.reflect("fresh", "anything")
        assert x.text is None and x.note == "bank is empty"


async def test_retain_is_async_and_returns_operation_id(adapter, api):
    r = await adapter.retain(BANK, "deployed 2.3.1", context="release", tags=["deploy"])
    body = httpx.Response(200, content=api.calls.last.request.content).json()
    assert body == {"items": [{"content": "deployed 2.3.1", "context": "release", "tags": ["deploy"]}], "async": True}
    assert r.accepted and r.operation_id == "op-1" and r.raw["status"] == "pending"


async def test_retain_omits_empty_optional_fields(adapter, api):
    await adapter.retain(BANK, "just this")
    body = httpx.Response(200, content=api.calls.last.request.content).json()
    assert body["items"] == [{"content": "just this"}]


async def test_reflect_passes_context(adapter, api):
    r = await adapter.reflect(BANK, "what do we know?", budget="mid", context="release review")
    body = httpx.Response(200, content=api.calls.last.request.content).json()
    assert body == {"query": "what do we know?", "budget": "mid", "context": "release review"}
    assert r.text.startswith("## What we know") and r.raw["based_on"] == {}


async def test_health_up_and_down(adapter, api):
    h = await adapter.health()
    assert h.ok and h.data["version"] == "0.9.2"
    api.get("/health").mock(side_effect=httpx.ConnectError("refused"))
    h = await adapter.health()
    assert not h.ok and "unreachable" in h.detail


async def test_server_errors_propagate(adapter, api):
    api.post(f"/v1/default/banks/{BANK}/memories/recall").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.recall(BANK, "x")


def test_units_ollama_defaults(adapter):
    (u,) = adapter.units()
    assert u.name == "memory" and u.role == "memory" and u.image == hindsight.IMAGE
    assert u.http_port == 8888 and u.health_path == "/health" and u.depends_on == ["postgres"]
    env = u.env
    assert env["HINDSIGHT_API_DATABASE_URL"] == "postgresql://cerebro:${POSTGRES_PASSWORD}@postgres:5432/hindsight"
    assert env["HINDSIGHT_API_LLM_PROVIDER"] == "ollama" and env["HINDSIGHT_API_LLM_BASE_URL"] == "http://ollama:11434"
    assert env["HINDSIGHT_API_LLM_MODEL"] == "gpt-oss:20b" and env["HINDSIGHT_API_LLM_API_KEY"] == "ollama"
    assert env["HINDSIGHT_API_LLM_OLLAMA_NUM_CTX"] == "32768"
    assert env["HINDSIGHT_API_EMBEDDINGS_PROVIDER"] == "openai"
    assert env["HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL"] == "http://ollama:11434/v1"
    assert env["HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL"] == "bge-m3" and env["HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS"] == "1024"
    assert env["HINDSIGHT_API_RERANKER_PROVIDER"] == "rrf"
    assert env["HINDSIGHT_API_TENANT_API_KEY"] == "${HINDSIGHT_API_KEY}" == env["HINDSIGHT_CP_DATAPLANE_API_KEY"]
    assert env["HINDSIGHT_API_TENANT_EXTENSION"].endswith("ApiKeyTenantExtension")
    assert set(u.secret_env) == {"POSTGRES_PASSWORD", "HINDSIGHT_API_KEY"}
    assert u.volumes == []          # v1 ran Hindsight with only a shm tmpfs; all state is in Postgres


def test_units_openai_provider(ctx):
    ctx.config.inference.llm = ModelEndpoint(provider="openai", base_url="https://llm.internal/v1", model="big", api_key_env="LLM_KEY")
    ctx.config.inference.embed = EmbedEndpoint(provider="openai", base_url="https://emb.internal/v1", model="e5", dim=768, api_key_env="EMB_KEY")
    (u,) = registry.build("memory", "hindsight", {"cp_access_key_secret": "HINDSIGHT_CP_ACCESS_KEY"}, ctx).units()
    env = u.env
    assert env["HINDSIGHT_API_LLM_PROVIDER"] == "openai" and env["HINDSIGHT_API_LLM_BASE_URL"] == "https://llm.internal/v1"
    assert env["HINDSIGHT_API_LLM_API_KEY"] == "${LLM_KEY}" and env["HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY"] == "${EMB_KEY}"
    assert env["HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL"] == "https://emb.internal/v1"      # not doubled
    assert "HINDSIGHT_API_LLM_OLLAMA_NUM_CTX" not in env
    assert env["HINDSIGHT_CP_ACCESS_KEY"] == "${HINDSIGHT_CP_ACCESS_KEY}"
    assert u.secret_env == ["POSTGRES_PASSWORD", "HINDSIGHT_API_KEY", "LLM_KEY", "EMB_KEY", "HINDSIGHT_CP_ACCESS_KEY"]


def test_base_url_comes_from_locator_or_option(ctx):
    assert registry.build("memory", "hindsight", {}, ctx).base_url == BASE
    assert registry.build("memory", "hindsight", {"url": "http://hs.local:8888/"}, ctx).base_url == "http://hs.local:8888"

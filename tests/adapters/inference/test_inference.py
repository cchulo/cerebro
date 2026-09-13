"""openai_compat and ollama inference adapters against respx-mocked servers (no real model is ever called)."""
import httpx, pytest, respx
from cerebro.core import registry, Health
from cerebro.core.config import ModelEndpoint, EmbedEndpoint
from cerebro.core.contracts import Inference, ChatMessage
from cerebro.adapters.inference.openai_compat import openai_base
from cerebro.adapters.inference.ollama import ollama_root

OLLAMA = "http://ollama:11434"
MSGS = [ChatMessage(role="system", content="be brief"), ChatMessage(role="user", content="hi")]


def body(call):
    return httpx.Response(200, content=call.request.content).json()


# ------------------------------------------------------------------------------------------------- openai_compat
@pytest.fixture
def oa(ctx):
    return registry.build("inference", "openai_compat", {}, ctx)


@pytest.fixture
def oa_api():
    with respx.mock(base_url=OLLAMA, assert_all_called=False) as m:
        m.post("/v1/chat/completions").mock(return_value=httpx.Response(200, json={
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}}]}))
        m.post("/v1/embeddings").mock(return_value=httpx.Response(200, json={
            "data": [{"index": 1, "embedding": [0.3, 0.4]}, {"index": 0, "embedding": [0.1, 0.2]}]}))
        m.get("/v1/models").mock(return_value=httpx.Response(200, json={"data": [{"id": "gpt-oss:20b"}, {"id": "bge-m3"}]}))
        yield m


def test_openai_compat_is_inference(oa):
    assert isinstance(oa, Inference) and oa.kind == "inference" and oa.name == "openai_compat"


async def test_openai_chat_appends_v1_for_ollama_and_sends_placeholder_key(oa, oa_api):
    out = await oa.chat(MSGS, temperature=0.2, max_tokens=64)
    assert out == "hello"
    req = oa_api.calls.last.request
    assert req.url == f"{OLLAMA}/v1/chat/completions" and req.headers["authorization"] == "Bearer ollama"
    assert body(oa_api.calls.last) == {"model": "gpt-oss:20b", "temperature": 0.2, "max_tokens": 64,
                                       "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]}


async def test_openai_embed_orders_by_index(oa, oa_api):
    assert await oa.embed(["a", "b"]) == [[0.1, 0.2], [0.3, 0.4]]
    assert body(oa_api.calls.last) == {"model": "bge-m3", "input": ["a", "b"]}
    assert await oa.embed([]) == [] and len(oa_api.calls) == 1


async def test_openai_health(oa, oa_api):
    h = await oa.health()
    assert isinstance(h, Health) and h.ok and "gpt-oss:20b" in h.data["models"]
    oa_api.get("/v1/models").mock(return_value=httpx.Response(401))
    assert not (await oa.health()).ok
    oa_api.get("/v1/models").mock(side_effect=httpx.ConnectError("no"))
    assert "unreachable" in (await oa.health()).detail


async def test_openai_provider_uses_secret_and_url_as_given(ctx):
    ctx.secrets.values["LLM_KEY"] = "sk-1"
    ctx.config.inference.llm = ModelEndpoint(provider="openai", base_url="https://llm.internal/v1/", model="big", api_key_env="LLM_KEY")
    ctx.config.inference.embed = EmbedEndpoint(provider="openai", base_url="https://llm.internal/v1", model="e5", dim=2, api_key_env="LLM_KEY")
    a = registry.build("inference", "openai_compat", {}, ctx)
    assert openai_base(a.llm) == "https://llm.internal/v1" and a.configured()
    with respx.mock(base_url="https://llm.internal") as m:
        m.post("/v1/chat/completions").mock(return_value=httpx.Response(200, json={"choices": []}))
        assert await a.chat(MSGS) == ""
        assert m.calls.last.request.headers["authorization"] == "Bearer sk-1"
        assert body(m.calls.last)["model"] == "big"


def test_openai_provider_without_key_is_not_configured(ctx):
    ctx.config.inference.llm = ModelEndpoint(provider="openai", base_url="https://llm.internal/v1", model="big", api_key_env="MISSING")
    assert not registry.build("inference", "openai_compat", {}, ctx).configured()


def test_openai_base_does_not_double_v1():
    assert openai_base(ModelEndpoint(provider="ollama", base_url="http://x:11434/v1", model="m")) == "http://x:11434/v1"


async def test_openai_errors_propagate(oa, oa_api):
    oa_api.post("/v1/chat/completions").mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await oa.chat(MSGS)


# ------------------------------------------------------------------------------------------------- ollama native
@pytest.fixture
def ol(ctx):
    return registry.build("inference", "ollama", {"keep_alive": "10m"}, ctx)


@pytest.fixture
def ol_api():
    with respx.mock(base_url=OLLAMA, assert_all_called=False) as m:
        m.post("/api/chat").mock(return_value=httpx.Response(200, json={"message": {"role": "assistant", "content": "hey"}, "done": True}))
        m.post("/api/embed").mock(return_value=httpx.Response(200, json={"model": "bge-m3", "embeddings": [[0.1, 0.2], [0.3, 0.4]]}))
        m.get("/api/tags").mock(return_value=httpx.Response(200, json={"models": [{"name": "gpt-oss:20b"}, {"name": "bge-m3:latest"}]}))
        yield m


def test_ollama_is_inference(ol):
    assert isinstance(ol, Inference) and ol.name == "ollama"


async def test_ollama_chat_native(ol, ol_api):
    assert await ol.chat(MSGS, temperature=0.5, max_tokens=32) == "hey"
    req = ol_api.calls.last.request
    assert req.url == f"{OLLAMA}/api/chat" and "authorization" not in req.headers
    assert body(ol_api.calls.last) == {"model": "gpt-oss:20b", "stream": False, "keep_alive": "10m",
                                       "options": {"temperature": 0.5, "num_predict": 32},
                                       "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]}


async def test_ollama_embed_native(ol, ol_api):
    assert await ol.embed(["a", "b"], model="other") == [[0.1, 0.2], [0.3, 0.4]]
    assert body(ol_api.calls.last) == {"model": "other", "input": ["a", "b"], "keep_alive": "10m"}


async def test_ollama_health_reports_missing_models(ol, ol_api):
    h = await ol.health()
    assert h.ok and "missing" not in h.data
    ol_api.get("/api/tags").mock(return_value=httpx.Response(200, json={"models": [{"name": "bge-m3:latest"}]}))
    assert (await ol.health()).data["missing"] == ["gpt-oss:20b"]
    ol_api.get("/api/tags").mock(side_effect=httpx.ConnectError("no"))
    assert not (await ol.health()).ok


def test_ollama_root_strips_v1(ctx):
    assert ollama_root(ModelEndpoint(base_url="http://x:11434/v1/", model="m")) == "http://x:11434"
    assert ollama_root(ModelEndpoint(base_url="http://x:11434", model="m")) == "http://x:11434"

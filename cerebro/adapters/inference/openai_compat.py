"""Inference over the OpenAI chat / embeddings API: any /v1-compatible server the organisation controls (vLLM,
llama.cpp, LiteLLM, Ollama's compatibility layer, a hosted endpoint the org has approved).

    POST {base}/chat/completions   {model, messages, temperature, max_tokens?}   -> choices[0].message.content
    POST {base}/embeddings         {model, input: [...]}                          -> data[i].embedding (by index)
    GET  {base}/models             (health)

`base` is the endpoint's base_url; when the provider is `ollama` and the URL has no `/v1`, `/v1` is appended (Ollama
serves the OpenAI API under it). The API key is the secret named by `api_key_env`; for Ollama the placeholder
"ollama" is sent when none is configured, because OpenAI clients reject an empty bearer.

Options (inference.options are not a thing in the schema; these come from the adapter's constructor options, i.e.
whoever builds it): timeout (seconds, default 120).
"""
from __future__ import annotations
from typing import Any
import httpx
from cerebro.core.config import ModelEndpoint
from cerebro.core.contracts.inference import Inference, ChatMessage
from cerebro.core.types import Health
from ._common import ClientCache, api_key


def openai_base(ep: ModelEndpoint) -> str:
    url = ep.base_url.rstrip("/")
    if ep.provider == "ollama" and not url.endswith("/v1"):
        url += "/v1"
    return url


class Adapter(Inference):
    name = "openai_compat"

    def __init__(self, options=None, ctx=None):
        super().__init__(options, ctx)
        self.timeout = float(self.option("timeout", 120))
        self._clients = ClientCache()

    @property
    def llm(self) -> ModelEndpoint:
        return self.ctx.config.inference.llm

    @property
    def embed_ep(self) -> ModelEndpoint:
        return self.ctx.config.inference.embed

    def configured(self) -> bool:
        return all(api_key(self, ep) is not None for ep in (self.llm, self.embed_ep))

    def _client(self, ep: ModelEndpoint) -> httpx.AsyncClient:
        return self._clients.get(openai_base(ep), api_key(self, ep), self.timeout)

    async def aclose(self) -> None:
        await self._clients.aclose()

    async def chat(self, messages: list[ChatMessage], *, model: str | None = None, temperature: float = 0.0,
                   max_tokens: int | None = None) -> str:
        ep = self.llm
        body: dict[str, Any] = {"model": model or ep.model, "temperature": temperature,
                                "messages": [{"role": m.role, "content": m.content} for m in messages], **ep.options}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        r = await self._client(ep).post("/chat/completions", json=body)
        r.raise_for_status()
        choices = r.json().get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        return msg.get("content") or ""

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        if not texts:
            return []
        ep = self.embed_ep
        r = await self._client(ep).post("/embeddings", json={"model": model or ep.model, "input": list(texts), **ep.options})
        r.raise_for_status()
        data = r.json().get("data") or []
        data = sorted(data, key=lambda d: d.get("index", 0))
        return [list(d["embedding"]) for d in data]

    async def health(self) -> Health:
        ep = self.llm
        try:
            r = await self._client(ep).get("/models")
            if r.status_code >= 400:
                return Health.down(f"{openai_base(ep)}/models -> {r.status_code}", status=r.status_code)
            models = [m.get("id") for m in (r.json().get("data") or []) if isinstance(m, dict)]
            data = {"models": models} if models else {}
            if models and ep.model not in models:
                data["missing"] = ep.model
            return Health.up(f"openai-compatible at {openai_base(ep)}", **data)
        except httpx.HTTPError as e:
            return Health.down(f"{openai_base(ep)} unreachable: {e}")

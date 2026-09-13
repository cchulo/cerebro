"""Inference over Ollama's native API (https://github.com/ollama/ollama/blob/main/docs/api.md).

    POST {root}/api/chat    {model, messages, stream: false, options: {temperature, num_predict?}} -> message.content
    POST {root}/api/embed   {model, input: [...]}                                                  -> embeddings
    GET  {root}/api/tags    (health; lists the pulled models)

`root` is the endpoint's base_url with any trailing `/v1` removed, so the same cerebro.yaml works for this adapter
and for `openai_compat`. Ollama has no authentication; a bearer is still sent when `api_key_env` names a secret,
for installs behind an authenticating proxy. Endpoints whose provider is not `ollama` are used as given.

Options: timeout (seconds, default 120), keep_alive (passed through to Ollama when set).
"""
from __future__ import annotations
from typing import Any
import httpx
from cerebro.core.config import ModelEndpoint
from cerebro.core.contracts.inference import Inference, ChatMessage
from cerebro.core.types import Health
from ._common import ClientCache


def ollama_root(ep: ModelEndpoint) -> str:
    url = ep.base_url.rstrip("/")
    return url[: -len("/v1")] if url.endswith("/v1") else url


class Adapter(Inference):
    name = "ollama"

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

    def _client(self, ep: ModelEndpoint) -> httpx.AsyncClient:
        key = self.ctx.secret(ep.api_key_env) if self.ctx else None
        return self._clients.get(ollama_root(ep), key, self.timeout)

    async def aclose(self) -> None:
        await self._clients.aclose()

    def _extra(self) -> dict[str, Any]:
        ka = self.option("keep_alive")
        return {"keep_alive": ka} if ka is not None else {}

    async def chat(self, messages: list[ChatMessage], *, model: str | None = None, temperature: float = 0.0,
                   max_tokens: int | None = None) -> str:
        ep = self.llm
        opts: dict[str, Any] = {"temperature": temperature, **ep.options}
        if max_tokens is not None:
            opts["num_predict"] = max_tokens
        body = {"model": model or ep.model, "stream": False, "options": opts,
                "messages": [{"role": m.role, "content": m.content} for m in messages], **self._extra()}
        r = await self._client(ep).post("/api/chat", json=body)
        r.raise_for_status()
        return (r.json().get("message") or {}).get("content") or ""

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        if not texts:
            return []
        ep = self.embed_ep
        body = {"model": model or ep.model, "input": list(texts), **self._extra()}
        if ep.options:
            body["options"] = dict(ep.options)
        r = await self._client(ep).post("/api/embed", json=body)
        r.raise_for_status()
        return [list(v) for v in (r.json().get("embeddings") or [])]

    async def health(self) -> Health:
        ep = self.llm
        try:
            r = await self._client(ep).get("/api/tags")
            if r.status_code >= 400:
                return Health.down(f"{ollama_root(ep)}/api/tags -> {r.status_code}", status=r.status_code)
            names = [m.get("name") for m in (r.json().get("models") or []) if isinstance(m, dict)]
            data: dict[str, Any] = {"models": names}
            wanted = {ep.model, self.embed_ep.model}
            missing = sorted(w for w in wanted if names and w not in names and f"{w}:latest" not in names)
            if missing:
                data["missing"] = missing
            return Health.up(f"ollama at {ollama_root(ep)}", **data)
        except httpx.HTTPError as e:
            return Health.down(f"{ollama_root(ep)} unreachable: {e}")

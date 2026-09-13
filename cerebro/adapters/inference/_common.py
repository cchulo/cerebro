"""Shared helpers for the inference adapters: which endpoint to use, its API key, one httpx client per adapter."""
from __future__ import annotations
import httpx
from cerebro.core.config import ModelEndpoint
from cerebro.core.context import Adapter

OLLAMA_PLACEHOLDER_KEY = "ollama"       # OpenAI-compatible servers reject an empty key; Ollama ignores the value


def api_key(adapter: Adapter, ep: ModelEndpoint) -> str | None:
    """The bearer for an endpoint: the secret named by api_key_env, else the placeholder for Ollama, else None."""
    key = adapter.ctx.secret(ep.api_key_env) if adapter.ctx else None
    if key:
        return key
    return OLLAMA_PLACEHOLDER_KEY if ep.provider == "ollama" else None


def make_client(base_url: str, key: str | None, timeout: float) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout)


class ClientCache:
    """One AsyncClient per base URL (llm and embed endpoints usually differ only by model)."""
    def __init__(self):
        self._clients: dict[tuple[str, str | None], httpx.AsyncClient] = {}

    def get(self, base_url: str, key: str | None, timeout: float) -> httpx.AsyncClient:
        k = (base_url, key)
        c = self._clients.get(k)
        if c is None or c.is_closed:
            c = self._clients[k] = make_client(base_url, key, timeout)
        return c

    async def aclose(self) -> None:
        for c in self._clients.values():
            if not c.is_closed:
                await c.aclose()

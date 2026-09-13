"""Inference: the one outbound path for text that cerebro itself sends to a model (engines are configured to the
same endpoints through their unit env). Most adapters never need this; the pgvector docs adapter does (embeddings)."""
from __future__ import annotations
from abc import abstractmethod
from pydantic import BaseModel
from ..context import Adapter
from ..types import Health


class ChatMessage(BaseModel):
    role: str
    content: str


class Inference(Adapter):
    kind = "inference"

    @abstractmethod
    async def chat(self, messages: list[ChatMessage], *, model: str | None = None, temperature: float = 0.0,
                   max_tokens: int | None = None) -> str: ...

    @abstractmethod
    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]: ...

    @abstractmethod
    async def health(self) -> Health: ...

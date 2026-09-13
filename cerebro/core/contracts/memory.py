"""MemoryStore: what agents learned by doing, addressed by bank. Policy decides which banks a caller may use;
the store only ever sees a bank name."""
from __future__ import annotations
from abc import abstractmethod
from typing import Any
from pydantic import BaseModel, Field
from ..context import Adapter
from ..types import Health

BUDGETS = ("low", "mid", "high")


class Memory(BaseModel):
    id: str | None = None
    content: str
    context: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: str | None = None
    score: float | None = None


class RecallResult(BaseModel):
    bank: str
    results: list[Memory] = Field(default_factory=list)
    note: str | None = None
    raw: Any | None = None


class RetainResult(BaseModel):
    bank: str
    accepted: bool = True
    operation_id: str | None = None
    raw: Any | None = None


class ReflectResult(BaseModel):
    bank: str
    text: str | None = None
    note: str | None = None
    raw: Any | None = None


class MemoryStore(Adapter):
    kind = "memory"
    supports_reflect: bool = True

    @abstractmethod
    async def recall(self, bank: str, query: str, *, budget: str = "mid", max_tokens: int = 4096) -> RecallResult: ...

    @abstractmethod
    async def retain(self, bank: str, content: str, *, context: str | None = None,
                     tags: list[str] | None = None) -> RetainResult: ...

    async def reflect(self, bank: str, query: str, *, budget: str = "low", context: str | None = None) -> ReflectResult:
        from ..types import Unsupported
        raise Unsupported(f"{self.name} does not support reflect")

    @abstractmethod
    async def health(self) -> Health: ...

    @staticmethod
    def check_budget(budget: str) -> str:
        if budget not in BUDGETS:
            raise ValueError(f"budget must be one of {BUDGETS}")
        return budget

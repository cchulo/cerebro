"""DocumentIndex: one index per scope, fed by the ingest engine, queried by the gateway.

Isolation rule: an adapter must never merge documents across scopes. The `scope` argument names which index.
"""
from __future__ import annotations
from abc import abstractmethod
from typing import Any
from pydantic import BaseModel, Field
from ..context import Adapter
from ..types import Health


class DocumentText(BaseModel):
    source_id: str                  # "<plugin>:<scope>:<doc.key>", unique across the stack
    text: str
    title: str = ""


class Batch(BaseModel):
    """Changes for ONE scope. The adapter decides ordering (deletes first, inserts together, idle waits)."""
    scope: str
    deletes: set[str] = Field(default_factory=set)
    upserts: list[DocumentText] = Field(default_factory=list)

    def upsert(self, source_id: str, text: str, title: str = "") -> None:
        self.deletes.add(source_id)                   # any previous version goes first
        self.upserts.append(DocumentText(source_id=source_id, text=text, title=title))

    def delete(self, source_id: str) -> None:
        self.deletes.add(source_id)

    @property
    def empty(self) -> bool:
        return not self.deletes and not self.upserts


class ApplyReport(BaseModel):
    scope: str
    deleted: int = 0
    inserted: int = 0
    note: str | None = None


class QueryOptions(BaseModel):
    mode: str = "default"           # adapter-specific; must be one of DocumentIndex.modes
    top_k: int = 10
    extra: dict[str, Any] = Field(default_factory=dict)


class Reference(BaseModel):
    id: str | None = None
    source: str                     # decoded source id or URL the answer can cite
    title: str | None = None
    url: str | None = None
    excerpt: str | None = None


class DocAnswer(BaseModel):
    scope: str
    answer: str = ""                # synthesised answer, or the concatenated passages for retrieval-only engines
    answered: bool = False          # False = index had nothing useful; the gateway may fall back to live sources
    references: list[Reference] = Field(default_factory=list)
    raw: dict[str, Any] | None = None


class DocumentIndex(Adapter):
    kind = "docs"
    modes: tuple[str, ...] = ("default",)
    default_mode: str = "default"

    @abstractmethod
    async def apply(self, scope: str, batch: Batch) -> ApplyReport: ...

    @abstractmethod
    async def query(self, scope: str, query: str, opts: QueryOptions | None = None) -> DocAnswer: ...

    @abstractmethod
    async def health(self, scope: str) -> Health: ...

    async def stats(self, scope: str) -> dict:
        """Optional progress figures (documents processed, pending) for status screens."""
        return {}

    def check_mode(self, mode: str | None) -> str:
        m = mode or self.default_mode
        if m == "default":
            m = self.default_mode
        if m not in self.modes and m != self.default_mode:
            raise ValueError(f"mode must be one of {self.modes}")
        return m

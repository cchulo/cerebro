"""CodeIntelligence: search and graph over the repositories of ONE unit.

A unit is a scope's repos side by side, or one repository (cerebro.core.units). Adapters expose two capabilities,
`search` (text/symbol hits with file and line, plus per-repository diagnostics in SearchResult.errors) and `graph`
(engine tools proxied by name), and declare which they have through `capabilities()`. Branch support is a
capability too: engines without it reject a `branch` argument with cerebro.core.types.Unsupported.
"""
from __future__ import annotations
from abc import abstractmethod
from typing import Any
from pydantic import BaseModel, Field
from ..context import Adapter
from ..types import Health
from ..units import CodeUnit


class ToolInfo(BaseModel):
    name: str
    description: str = ""
    read_only: bool = True
    input_schema: dict[str, Any] | None = None


class Capabilities(BaseModel):
    engine: str
    version: str | None = None
    search: bool = False
    graph: bool = False
    branches: bool = False          # per-branch graphs selectable per query
    multi_root: bool = False        # one call may address several repos in the unit
    tools: list[ToolInfo] = Field(default_factory=list)

    def tool_names(self) -> set[str]:
        return {t.name for t in self.tools}


class SearchHit(BaseModel):
    repository: str                 # github.com/org/x form
    path: str
    line: int | None = None
    content: str = ""
    language: str | None = None
    url: str | None = None
    branch: str | None = None


class SearchResult(BaseModel):
    """What one unit answered: the hits, plus per-repository diagnostics for the repositories that could not be
    searched (not checked out yet, branch not indexed). Key "*" is an error of the unit as a whole. A unit that
    answers nothing must say why here rather than return an empty list."""
    hits: list[SearchHit] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)

    def __iter__(self):                    # iterating a result yields its hits
        return iter(self.hits)

    def __len__(self) -> int:
        return len(self.hits)


class ToolResult(BaseModel):
    unit: str
    tool: str
    is_error: bool = False
    content: list[Any] = Field(default_factory=list)
    structured: Any | None = None


class CodeIntelligence(Adapter):
    kind = "code"

    @abstractmethod
    async def capabilities(self, unit: CodeUnit) -> Capabilities: ...

    @abstractmethod
    async def search(self, unit: CodeUnit, query: str, *, repos: list[str] | None = None, branch: str | None = None,
                     regex: bool = False, max_results: int = 20) -> SearchResult:
        """Text / symbol search inside the unit; `repos` narrows to those repository names, never widens.
        Repositories that could not be searched are reported in SearchResult.errors, not silently skipped."""

    @abstractmethod
    async def call(self, unit: CodeUnit, tool: str, args: dict[str, Any] | None = None, *,
                   branch: str | None = None) -> ToolResult:
        """Proxy one READ-ONLY engine tool. Adapters enforce their own allowlist before forwarding."""

    @abstractmethod
    async def health(self, unit: CodeUnit) -> Health: ...

    def index_job_name(self, unit: CodeUnit) -> str:
        return f"index-{unit.name}"

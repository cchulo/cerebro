"""In-process fakes of the engine contracts for gateway tests. They record what the gateway asked for and answer
deterministically; some deliberately misbehave (a code engine leaking a foreign repository) so the gateway's
second-pass filters are exercised."""
from typing import Any
from cerebro.core import Health, CodeUnit
from cerebro.core.contracts import (DocumentIndex, Batch, ApplyReport, QueryOptions, DocAnswer, Reference,
                                    CodeIntelligence, Capabilities, ToolInfo, ToolResult, SearchHit,
                                    MemoryStore, RecallResult, RetainResult, ReflectResult, Memory,
                                    LiveSource, Plugin)
from cerebro.core.types import Forbidden, Unsupported


class FakeDocs(DocumentIndex):
    """Answers per scope: {"public": ("text", True)}; unlisted scopes answer nothing (answered=False)."""
    name = "fake-docs"
    modes = ("mix", "naive", "local")
    default_mode = "mix"

    def __init__(self, options=None, ctx=None, answers: dict[str, tuple[str, bool]] | None = None):
        super().__init__(options, ctx)
        self.answers = answers or {}
        self.calls: list[tuple[str, str, str]] = []

    async def apply(self, scope: str, batch: Batch) -> ApplyReport:
        if batch.scope != scope:
            raise ValueError("batch is for another scope")
        return ApplyReport(scope=scope)

    async def query(self, scope: str, query: str, opts: QueryOptions | None = None) -> DocAnswer:
        opts = opts or QueryOptions()
        self.calls.append((scope, query, opts.mode))
        text, answered = self.answers.get(scope, ("", False))
        refs = [Reference(id="1", source=f"confluence:{scope}:ENG/1", title="one")] if answered else []
        return DocAnswer(scope=scope, answer=text, answered=answered, references=refs)

    async def health(self, scope: str) -> Health:
        return Health.up()


class FakeCode(CodeIntelligence):
    """One hit per repository the gateway asks for, plus a LEAKED hit from a repository outside the unit that the
    gateway must drop. No branch support, so `branch` raises Unsupported. Graph tools: `stats` only."""
    name = "fake-code"
    LEAK = "github.com/pallets/werkzeug"

    def __init__(self, options=None, ctx=None):
        super().__init__(options, ctx)
        self.searches: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str, dict]] = []

    async def capabilities(self, unit: CodeUnit) -> Capabilities:
        return Capabilities(engine="fake", version="0", search=True, graph=True, branches=False,
                            tools=[ToolInfo(name="stats", description="repository stats")])

    async def search(self, unit, query, *, repos=None, branch=None, regex=False, max_results=20):
        self.searches.append({"unit": unit.name, "query": query, "repos": repos, "branch": branch, "regex": regex})
        if branch:
            raise Unsupported("fake engine keeps no per-branch index")
        names = repos or [r.name for r in unit.repos]
        hits = [SearchHit(repository=n, path="src/main.py", line=1, content=f"{query} in {n}") for n in names]
        hits.append(SearchHit(repository=self.LEAK, path="leak.py", line=9, content="should never leave the gateway"))
        return hits

    async def call(self, unit, tool, args=None, *, branch=None) -> ToolResult:
        self.calls.append((unit.name, tool, args or {}))
        if tool != "stats":
            raise Forbidden(f"tool '{tool}' is not allowed through the gateway")
        return ToolResult(unit=unit.name, tool=tool, content=[f"{len(unit.repos)} repos"], structured={"repos": len(unit.repos)})

    async def health(self, unit: CodeUnit) -> Health:
        return Health.up()


class FakeMemory(MemoryStore):
    name = "fake-memory"

    def __init__(self, options=None, ctx=None, supports_reflect: bool = True):
        super().__init__(options, ctx)
        self.supports_reflect = supports_reflect
        self.banks: dict[str, list[Memory]] = {}

    async def recall(self, bank, query, *, budget="mid", max_tokens=4096) -> RecallResult:
        items = [m for m in self.banks.get(bank, []) if query.lower() in m.content.lower()] or list(self.banks.get(bank, []))
        return RecallResult(bank=bank, results=items, note=None if items else "bank is empty")

    async def retain(self, bank, content, *, context=None, tags=None) -> RetainResult:
        self.banks.setdefault(bank, []).append(Memory(id=str(len(self.banks.get(bank, [])) + 1), content=content, context=context, tags=tags or []))
        return RetainResult(bank=bank, accepted=True, operation_id="op-1")

    async def reflect(self, bank, query, *, budget="low", context=None) -> ReflectResult:
        if not self.supports_reflect:
            raise Unsupported("fake memory cannot reflect")
        return ReflectResult(bank=bank, text=f"{len(self.banks.get(bank, []))} memories about {query}")

    async def health(self) -> Health:
        return Health.up()


class FakeConfluenceLive(LiveSource):
    """Returns one hit per allowed entry, tagged with the space it came from, so tests can see the confinement."""
    name = "confluence"
    calls: list[dict] = []

    async def search(self, query, allowed, limit=10):
        FakeConfluenceLive.calls.append({"query": query, "allowed": allowed, "limit": limit, "options": self.options})
        return [{"ref": f"{a['scope']}-{sp}-1", "space": sp, "scope": a["scope"], "title": f"{query} in {sp}"}
                for a in allowed for sp in a.get("spaces", [])][:limit]

    async def fetch(self, ref, allowed, max_chars=20000):
        scope = ref.split("-", 1)[0]
        if scope not in {a["scope"] for a in allowed}:
            raise Forbidden(f"{ref} is outside your scopes")
        return {"ref": ref, "text": "page body"[:max_chars]}


class FakeJamaLive(LiveSource):
    name = "jama"

    async def search(self, query, allowed, limit=10):
        return [{"ref": f"jama-{a['scope']}", "scope": a["scope"]} for a in allowed]

    async def fetch(self, ref, allowed, max_chars=20000):
        return {"ref": ref}


def fake_plugins() -> dict[str, Plugin]:
    return {"confluence": Plugin(name="confluence", live=FakeConfluenceLive),
            "jama": Plugin(name="jama", live=FakeJamaLive)}

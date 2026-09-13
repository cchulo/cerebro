"""Hindsight (https://github.com/vectorize-io/hindsight) as the MemoryStore.

Wire calls are the ones v1's gateway made against Hindsight 0.9.2, bank-addressed, tenant `default`:

    POST /v1/{tenant}/banks/{bank}/memories/recall   {query, budget, max_tokens}       -> {results: [...], ...}
    POST /v1/{tenant}/banks/{bank}/memories          {items: [{content, context?, tags?}], async: true}
                                                     -> {operation_id, status}  (extraction runs in the background)
    POST /v1/{tenant}/banks/{bank}/reflect           {query, budget, context?}         -> {text, ...}
    GET  /health

Every call carries `Authorization: Bearer <HINDSIGHT_API_KEY>`; the unit is started with the same value as its tenant
API key, so only the gateway can reach it. A 404 means the bank has never been written to: recall and reflect answer
"bank is empty" instead of failing. A recall item is Hindsight's RecallResult (`id`, `text`, `type`, `context`,
`tags`, `mentioned_at` / `occurred_start`, `scores`); it is mapped onto `Memory` and the whole response is kept in
`raw` so nothing the engine returns is lost.

Options (engines.memory.options):
    reranker      HINDSIGHT_API_RERANKER_PROVIDER: local (cross-encoder, weights fetched once) | rrf (no model). Default local.
    tenant        path tenant, default "default".
    timeout       seconds per call, default 300 (recall with budget high can be slow on a small model).
    num_ctx       HINDSIGHT_API_LLM_OLLAMA_NUM_CTX when the LLM provider is ollama, default 32768.
    url           override the base URL (default: the Locator's endpoint for unit `memory`).
    cp_access_key_secret   secret name for HINDSIGHT_CP_ACCESS_KEY (control-plane UI login); unset = not passed.
"""
from __future__ import annotations
import logging
from typing import Any
import httpx
from cerebro.core.contracts.memory import MemoryStore, Memory, RecallResult, RetainResult, ReflectResult
from cerebro.core.contracts.provision import UnitSpec, PortSpec
from cerebro.core.types import Health

log = logging.getLogger(__name__)

IMAGE = "ghcr.io/vectorize-io/hindsight:0.9.2"
PORT = 8888
UNIT = "memory"
API_KEY_SECRET = "HINDSIGHT_API_KEY"
POSTGRES_SECRET = "POSTGRES_PASSWORD"


def _score(item: dict) -> float | None:
    scores = item.get("scores")
    if isinstance(scores, dict):
        for key in ("final", "score", "combined", "semantic"):
            v = scores.get(key)
            if isinstance(v, (int, float)):
                return float(v)
        for v in scores.values():
            if isinstance(v, (int, float)):
                return float(v)
    v = item.get("score")
    return float(v) if isinstance(v, (int, float)) else None


def to_memory(item: dict) -> Memory:
    return Memory(
        id=item.get("id"),
        content=item.get("text") or item.get("content") or "",
        context=item.get("context"),
        tags=list(item.get("tags") or []),
        created_at=item.get("mentioned_at") or item.get("occurred_start") or item.get("created_at") or item.get("timestamp"),
        score=_score(item),
    )


class Adapter(MemoryStore):
    name = "hindsight"
    supports_reflect = True

    def __init__(self, options=None, ctx=None):
        super().__init__(options, ctx)
        self.tenant: str = str(self.option("tenant", "default"))
        self.timeout = float(self.option("timeout", 300))
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ wiring
    @property
    def base_url(self) -> str:
        url = self.option("url") or (self.ctx.locator.endpoint(UNIT) if self.ctx else f"http://{UNIT}:{PORT}")
        return str(url).rstrip("/")

    @property
    def api_key(self) -> str | None:
        return self.ctx.secret(API_KEY_SECRET) if self.ctx else None

    def configured(self) -> bool:
        return bool(self.base_url)

    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            self._client = httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def _bank_path(self, bank: str) -> str:
        return f"/v1/{self.tenant}/banks/{bank}"

    async def _call(self, method: str, path: str, **kw) -> Any | None:
        """JSON body, or None when the bank does not exist yet (404)."""
        r = await self.client().request(method, path, **kw)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json() if r.content else {}

    # ------------------------------------------------------------------ contract
    async def recall(self, bank: str, query: str, *, budget: str = "mid", max_tokens: int = 4096) -> RecallResult:
        self.check_budget(budget)
        res = await self._call("POST", f"{self._bank_path(bank)}/memories/recall",
                               json={"query": query, "budget": budget, "max_tokens": max_tokens})
        if res is None:
            return RecallResult(bank=bank, results=[], note="bank is empty")
        items = res.get("results") if isinstance(res, dict) else res
        return RecallResult(bank=bank, results=[to_memory(i) for i in (items or []) if isinstance(i, dict)], raw=res)

    async def retain(self, bank: str, content: str, *, context: str | None = None,
                     tags: list[str] | None = None) -> RetainResult:
        item: dict[str, Any] = {"content": content}
        if context:
            item["context"] = context
        if tags:
            item["tags"] = list(tags)
        # async: Hindsight queues extraction (an LLM call) and answers with an operation id; the agent never waits
        res = await self._call("POST", f"{self._bank_path(bank)}/memories", json={"items": [item], "async": True})
        res = res or {}
        return RetainResult(bank=bank, accepted=True, operation_id=res.get("operation_id"), raw=res)

    async def reflect(self, bank: str, query: str, *, budget: str = "low", context: str | None = None) -> ReflectResult:
        self.check_budget(budget)
        payload: dict[str, Any] = {"query": query, "budget": budget}
        if context:
            payload["context"] = context
        res = await self._call("POST", f"{self._bank_path(bank)}/reflect", json=payload)
        if res is None:
            return ReflectResult(bank=bank, text=None, note="bank is empty")
        text = res.get("text") if isinstance(res, dict) else None
        return ReflectResult(bank=bank, text=text, raw=res)

    async def health(self) -> Health:
        try:
            r = await self.client().get("/health")
            if r.status_code == 404:            # older builds answer on the root only
                r = await self.client().get("/")
            if r.status_code >= 400:
                return Health.down(f"hindsight {r.status_code}", status=r.status_code)
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            return Health.up("hindsight", **(data if isinstance(data, dict) else {"body": data}))
        except httpx.HTTPError as e:
            return Health.down(f"hindsight unreachable: {e}")

    # ------------------------------------------------------------------ what the provisioner runs
    def units(self) -> list[UnitSpec]:
        inf = self.ctx.config.inference
        llm, embed = inf.llm, inf.embed
        secrets = [POSTGRES_SECRET, API_KEY_SECRET]

        def key_for(ep) -> str:
            if ep.api_key_env:
                if ep.api_key_env not in secrets:
                    secrets.append(ep.api_key_env)
                return f"${{{ep.api_key_env}}}"
            return "ollama"                      # must be non-empty even for Ollama

        embed_url = embed.base_url.rstrip("/")
        if embed.provider == "ollama" and not embed_url.endswith("/v1"):
            embed_url += "/v1"                   # Hindsight speaks the OpenAI embeddings API; for Ollama that is <root>/v1

        env = {
            "HINDSIGHT_API_DATABASE_URL": f"postgresql://cerebro:${{{POSTGRES_SECRET}}}@postgres:5432/hindsight",
            "HINDSIGHT_API_LLM_PROVIDER": llm.provider,
            "HINDSIGHT_API_LLM_BASE_URL": llm.base_url.rstrip("/"),
            "HINDSIGHT_API_LLM_MODEL": llm.model,
            "HINDSIGHT_API_LLM_API_KEY": key_for(llm),
            "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "openai",
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL": embed_url,
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY": key_for(embed),
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL": embed.model,
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS": str(embed.dim),
            "HINDSIGHT_API_RERANKER_PROVIDER": str(self.option("reranker", "local")),
            "HINDSIGHT_API_WORKER_ID": "hindsight-1",          # stable id so in-flight tasks survive restarts
            "HINDSIGHT_API_TENANT_EXTENSION": "hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension",
            "HINDSIGHT_API_TENANT_API_KEY": f"${{{API_KEY_SECRET}}}",
            "HINDSIGHT_CP_DATAPLANE_API_URL": f"http://localhost:{PORT}",
            "HINDSIGHT_CP_DATAPLANE_API_KEY": f"${{{API_KEY_SECRET}}}",
        }
        if llm.provider == "ollama":
            env["HINDSIGHT_API_LLM_OLLAMA_NUM_CTX"] = str(self.option("num_ctx", 32768))
        cp_secret = self.option("cp_access_key_secret")
        if cp_secret:
            env["HINDSIGHT_CP_ACCESS_KEY"] = f"${{{cp_secret}}}"
            secrets.append(str(cp_secret))
        engine = self.ctx.config.engines.memory
        return [UnitSpec(
            name=UNIT, role="memory", image=IMAGE, env=env, secret_env=secrets,
            ports=[PortSpec(name="http", port=PORT), PortSpec(name="control-plane", port=9999)],
            health_path="/health", depends_on=["postgres"],
            resources=dict(engine.resources) if engine else {},
            idle_ttl=engine.idle_ttl if engine else None,
            labels={"cerebro.engine": "hindsight"},
        )]

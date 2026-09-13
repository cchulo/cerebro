"""LightRAG as a DocumentIndex: one LightRAG server per scope, ported from the v1 ingest client and gateway tool.
Verified against ghcr.io/hkuds/lightrag:v1.5.7; every quirk of that server stays inside this module.

Facts that shape the adapter:
- POST /documents/texts {texts, file_sources}: a file_source is the document's identity (dedup key, doc_id seed).
  LightRAG keeps only its basename (Path(file_source).name), so a source id must not contain "/": "/" is encoded
  as "|" on the way in and decoded on the way out (references, answer text).
- Inserting a file_source that already exists is refused with 409. There is no in-place update: delete, then insert.
- DELETE /documents/delete_document {doc_ids} runs in the background and answers status="busy" while the pipeline
  is processing anything. A batch is therefore applied as: wait -> list -> delete (pipeline idle) -> insert, in
  chunks of INSERT_CHUNK texts per request.
- There is no GET /documents; POST /documents/paginated lists documents (id, file_path, status).
- GET /documents/pipeline_status reports busy / scanning / pending_enqueues / destructive_busy.
- The API key travels as the X-API-Key header; /health is whitelisted.
- Query answers arrive as {response, references: [{reference_id, file_path}], llm_generated}. LightRAG marks its canned
  no-context reply with llm_generated=false, but when the graph holds *related* material it writes a real answer that
  says it has nothing specific; both are misses (`answered=False`) so the gateway can fall back to live sources.

Options (engines.docs.options): image, insert_chunk, idle_timeout, poll_interval, timeout, and the tuning knobs that
become unit env: max_async, max_parallel_insert, gleaning, think, max_output_tokens.
"""
from __future__ import annotations
import asyncio, logging, re, time
from typing import Any, AsyncIterator
from contextlib import asynccontextmanager
import httpx
from cerebro.core import Health, docs_unit_name
from cerebro.core.contracts.docs import DocumentIndex, Batch, ApplyReport, QueryOptions, Reference, DocAnswer
from cerebro.core.contracts.provision import UnitSpec, PortSpec, VolumeSpec

log = logging.getLogger("cerebro.adapters.docs.lightrag")

IMAGE = "ghcr.io/hkuds/lightrag:v1.5.7"
PORT = 9621
INSERT_CHUNK = 25          # texts per /documents/texts request (the server limit is 50 MiB per request)
IDLE_TIMEOUT = 3600        # seconds to wait for the pipeline before giving up on deletes
POLL_INTERVAL = 3.0        # seconds between pipeline_status polls
DELETE_ATTEMPTS = 200      # "busy" answers tolerated before a delete gives up
POSTGRES_UNIT = "postgres"
POSTGRES_DATABASE = "lightrag"
POSTGRES_USER = "cerebro"

_NO_ANSWER = re.compile(
    r"no (specific |direct |relevant |explicit |detailed )?(information|mention|details|data|record|documentation)|"
    r"(does|do) not (contain|mention|include|provide|cover|have)|not (mentioned|covered|found|available|present|described) in|"
    r"unable to (find|locate)|cannot (find|locate)|could not find|(don't|do not) have (any )?(information|details)|"
    r"no documents? (was|were|is|are)? ?(found|available)", re.I)
_UNSAFE_WS = re.compile(r"[^A-Za-z0-9_]")


def encode_source(source_id: str) -> str:
    """Source id -> LightRAG file_source. "/" would be dropped by basename(), so it becomes "|"."""
    return source_id.replace("/", "|")


def decode_source(file_path: str) -> str:
    return file_path.replace("|", "/")


def answered(body: dict) -> bool:
    """True when the index produced a real answer with references, not a "nothing about that" reply."""
    refs = body.get("references") or []
    answer = body.get("response") or ""
    return bool(body.get("llm_generated", True)) and bool(refs) and not _NO_ANSWER.search(answer[:600])


def workspace(scope: str) -> str:
    """LightRAG WORKSPACE for a scope: a-z A-Z 0-9 _ only, as v1 (`scope_<safe>`)."""
    return "scope_" + _UNSAFE_WS.sub("_", scope)


class Adapter(DocumentIndex):
    name = "lightrag"
    modes = ("local", "global", "hybrid", "mix", "naive")
    default_mode = "mix"

    # ---------------------------------------------------------------------------------------------- plumbing
    def _base_url(self, scope: str) -> str:
        if self.ctx is None:
            raise RuntimeError("lightrag adapter needs an AdapterContext (locator + secrets)")
        return self.ctx.locator.endpoint(docs_unit_name(scope))

    def _headers(self) -> dict[str, str]:
        key = self.ctx.secret("LIGHTRAG_API_KEY") if self.ctx else None
        return {"X-API-Key": key} if key else {}

    @asynccontextmanager
    async def _client(self, scope: str) -> AsyncIterator[httpx.AsyncClient]:
        """A fresh client per operation: the ingest drives apply() from a worker thread with its own event loop, so
        pooled connections must never outlive the loop they were opened on."""
        async with httpx.AsyncClient(base_url=self._base_url(scope), headers=self._headers(),
                                     timeout=float(self.option("timeout", 300))) as c:
            yield c

    def _unit_label(self, scope: str) -> str:
        return docs_unit_name(scope)

    # ---------------------------------------------------------------------------------------------- pipeline
    async def pipeline_status(self, c: httpx.AsyncClient) -> dict:
        r = await c.get("/documents/pipeline_status")
        r.raise_for_status()
        return r.json()

    async def wait_idle(self, c: httpx.AsyncClient, scope: str, *, full: bool = True) -> None:
        """full=True: wait until nothing is processing (required before a delete).
        full=False: wait only for the short enqueue window, so a listing sees documents inserted moments ago."""
        timeout = float(self.option("idle_timeout", IDLE_TIMEOUT))
        poll = float(self.option("poll_interval", POLL_INTERVAL))
        deadline = time.monotonic() + timeout
        while True:
            st = await self.pipeline_status(c)
            blocking = st.get("destructive_busy") or st.get("scanning") or st.get("pending_enqueues")
            if full:
                blocking = blocking or st.get("busy")
            if not blocking:
                return
            if time.monotonic() > deadline:
                raise TimeoutError(f"{self._unit_label(scope)} pipeline still busy after {timeout}s: {st.get('latest_message')}")
            await asyncio.sleep(poll)

    async def list_documents(self, c: httpx.AsyncClient) -> dict[str, list[str]]:
        """decoded source id -> LightRAG doc ids (any status)."""
        out: dict[str, list[str]] = {}
        page = 1
        while True:
            r = await c.post("/documents/paginated", json={"page": page, "page_size": 200})
            r.raise_for_status()
            body = r.json()
            for d in body.get("documents", []):
                out.setdefault(decode_source(d.get("file_path") or ""), []).append(d["id"])
            if not body.get("pagination", {}).get("has_next"):
                return out
            page += 1

    async def delete_ids(self, c: httpx.AsyncClient, scope: str, doc_ids: list[str]) -> None:
        if not doc_ids:
            return
        poll = float(self.option("poll_interval", POLL_INTERVAL))
        await self.wait_idle(c, scope)
        for _ in range(DELETE_ATTEMPTS):
            r = await c.request("DELETE", "/documents/delete_document",
                                json={"doc_ids": doc_ids, "delete_file": False, "delete_llm_cache": False})
            r.raise_for_status()
            body = r.json()
            status = body.get("status")
            if status == "deletion_started":
                await self.wait_idle(c, scope)
                return
            if status == "busy":
                await asyncio.sleep(poll)
                continue
            raise RuntimeError(f"{self._unit_label(scope)} refused delete: {body}")
        raise TimeoutError(f"{self._unit_label(scope)}: could not acquire the pipeline to delete {len(doc_ids)} documents")

    # ---------------------------------------------------------------------------------------------- contract
    async def apply(self, scope: str, batch: Batch) -> ApplyReport:
        """Deletes first (the pipeline must be idle), then every insert together in chunks. Raises before anything
        is reported if LightRAG refused, so the caller's sync state stays untouched."""
        if batch.scope != scope:
            raise ValueError(f"batch is for scope '{batch.scope}', not '{scope}'")
        if batch.empty:
            return ApplyReport(scope=scope)
        chunk_size = int(self.option("insert_chunk", INSERT_CHUNK))
        deleted = inserted = 0
        async with self._client(scope) as c:
            if batch.deletes:
                await self.wait_idle(c, scope, full=False)     # documents enqueued moments ago may not be listed yet
                index = await self.list_documents(c)
                ids = [i for s in sorted(batch.deletes) for i in index.get(s, [])]
                if ids:                                         # only a real deletion needs the pipeline fully idle
                    log.info("%s: deleting %d documents", self._unit_label(scope), len(ids))
                    await self.delete_ids(c, scope, ids)
                    deleted = len(ids)
            texts = [(d.source_id, f"# {d.title}\n\n{d.text}" if d.title else d.text) for d in batch.upserts]
            for i in range(0, len(texts), chunk_size):
                chunk = texts[i:i + chunk_size]
                r = await c.post("/documents/texts", json={"texts": [t for _, t in chunk],
                                                           "file_sources": [encode_source(s) for s, _ in chunk]})
                if r.status_code == 409:
                    detail = r.json().get("detail") if "json" in r.headers.get("content-type", "") else r.text
                    raise RuntimeError(f"{self._unit_label(scope)}: {detail}")
                r.raise_for_status()
                inserted += len(chunk)
        log.info("%s: deleted %d, inserted %d (processing continues in the background)",
                 self._unit_label(scope), deleted, inserted)
        return ApplyReport(scope=scope, deleted=deleted, inserted=inserted,
                           note="accepted; extraction continues in the background" if inserted else None)

    async def query(self, scope: str, query: str, opts: QueryOptions | None = None) -> DocAnswer:
        """POST /query with include_references. The contract's `top_k` is not forwarded: LightRAG's top_k counts
        entities/relations, not passages, and its server default is the verified setting; pass it via opts.extra."""
        opts = opts or QueryOptions()
        mode = self.check_mode(opts.mode)
        payload: dict[str, Any] = {"query": query, "mode": mode, "include_references": True, **opts.extra}
        async with self._client(scope) as c:
            r = await c.post("/query", json=payload)
            r.raise_for_status()
            body = r.json()
        refs = body.get("references") or []
        answer = body.get("response") or ""
        for ref in refs:                                  # the answer text cites the encoded ids too
            fp = ref.get("file_path") or ""
            if fp:
                answer = answer.replace(fp, decode_source(fp))
        return DocAnswer(scope=scope, answer=answer, answered=answered(body),
                         references=[Reference(id=ref.get("reference_id"), source=decode_source(ref.get("file_path") or ""))
                                     for ref in refs],
                         raw=body)

    async def health(self, scope: str) -> Health:
        try:
            async with self._client(scope) as c:
                r = await c.get("/health", timeout=5)
        except httpx.HTTPError as e:
            return Health.down(f"{self._unit_label(scope)}: {type(e).__name__}: {e}".rstrip(": "))
        if r.status_code != 200:
            return Health.down(f"{self._unit_label(scope)}: HTTP {r.status_code}")
        body = r.json() if "json" in r.headers.get("content-type", "") else {}
        return Health.up(status=body.get("status", "healthy"), core_version=body.get("core_version"),
                         api_version=body.get("api_version"))

    async def stats(self, scope: str) -> dict:
        """Documents by status (PENDING / PROCESSING / PROCESSED / FAILED) plus the pipeline's own status."""
        async with self._client(scope) as c:
            st = await self.pipeline_status(c)
            r = await c.post("/documents/paginated", json={"page": 1, "page_size": 200})
            r.raise_for_status()
            body = r.json()
            counts: dict[str, int] = dict(body.get("status_counts") or {})
            if not counts:                                # older servers: count the listing ourselves
                page = 1
                while True:
                    for d in body.get("documents", []):
                        counts[d.get("status", "UNKNOWN")] = counts.get(d.get("status", "UNKNOWN"), 0) + 1
                    if not body.get("pagination", {}).get("has_next"):
                        break
                    page += 1
                    r = await c.post("/documents/paginated", json={"page": page, "page_size": 200})
                    r.raise_for_status()
                    body = r.json()
        return {"documents": counts, "total": sum(counts.values()), "busy": bool(st.get("busy")),
                "pending_enqueues": bool(st.get("pending_enqueues")), "latest_message": st.get("latest_message")}

    # ---------------------------------------------------------------------------------------------- units
    def _inference_env(self) -> tuple[dict[str, str], list[str]]:
        inf = self.ctx.config.inference
        env = {
            "LLM_BINDING": inf.llm.provider, "LLM_BINDING_HOST": inf.llm.base_url, "LLM_MODEL": inf.llm.model,
            "EMBEDDING_BINDING": inf.embed.provider, "EMBEDDING_BINDING_HOST": inf.embed.base_url,
            "EMBEDDING_MODEL": inf.embed.model, "EMBEDDING_DIM": str(inf.embed.dim),
        }
        secrets: list[str] = []
        for var, ep in (("LLM_BINDING_API_KEY", inf.llm), ("EMBEDDING_BINDING_API_KEY", inf.embed)):
            if ep.api_key_env:
                env[var] = "${" + ep.api_key_env + "}"     # provisioner resolves ${SECRET} from secret_env
                if ep.api_key_env not in secrets:
                    secrets.append(ep.api_key_env)
            else:
                env[var] = ep.provider                     # ollama / openai-compatible servers that ignore the key
        return env, secrets

    def units(self) -> list[UnitSpec]:
        """One LightRAG server per scope, named docs-<scope>, storage in the shared `postgres` unit."""
        if self.ctx is None:
            return []
        cfg = self.ctx.config
        engine = cfg.engines.docs
        opts = self.options
        knobs = {
            "MAX_ASYNC": str(opts.get("max_async", 2)),
            "MAX_PARALLEL_INSERT": str(opts.get("max_parallel_insert", 1)),
            "MAX_GLEANING": str(opts.get("gleaning", 0)),      # 0 = one extraction pass per chunk (halves LLM calls)
            "OLLAMA_LLM_THINK": str(bool(opts.get("think", False))).lower(),
            "OLLAMA_LLM_NUM_PREDICT": str(opts.get("max_output_tokens", 4096)),
            # query-answer cache OFF: it returns the old answer verbatim after a sync added the very document asked
            # about (verified on 1.5.7). The per-chunk extraction cache stays on: re-syncs stay cheap.
            "ENABLE_LLM_CACHE": "false",
        }
        inference_env, inference_secrets = self._inference_env()
        base_env = {
            "HOST": "0.0.0.0", "PORT": str(PORT),
            "WORKING_DIR": "/app/data/rag_storage", "INPUT_DIR": "/app/data/inputs",
            **inference_env,
            "OLLAMA_LLM_NUM_CTX": "32768", "LLM_TIMEOUT": "600",
            **knobs,
            "WHITELIST_PATHS": "/health",
            "LIGHTRAG_KV_STORAGE": "PGKVStorage", "LIGHTRAG_DOC_STATUS_STORAGE": "PGDocStatusStorage",
            "LIGHTRAG_VECTOR_STORAGE": "PGVectorStorage", "LIGHTRAG_GRAPH_STORAGE": "PGTableGraphStorage",
            "POSTGRES_HOST": POSTGRES_UNIT, "POSTGRES_PORT": "5432", "POSTGRES_USER": POSTGRES_USER,
            "POSTGRES_DATABASE": POSTGRES_DATABASE, "POSTGRES_VECTOR_INDEX_TYPE": "HNSW",
        }
        out: list[UnitSpec] = []
        for scope in cfg.scopes:
            out.append(UnitSpec(
                name=docs_unit_name(scope), role="docs", image=opts.get("image", IMAGE),
                env={**base_env, "WORKSPACE": workspace(scope)},
                secret_env=["LIGHTRAG_API_KEY", "POSTGRES_PASSWORD", *inference_secrets],
                ports=[PortSpec(port=PORT)],
                volumes=[VolumeSpec(name="data", mount_path="/app/data", size=str(engine.resources.get("storage", "5Gi")) if engine else "5Gi")],
                resources={k: v for k, v in (engine.resources if engine else {}).items() if k != "storage"},
                health_path="/health", scope=scope, idle_ttl=engine.idle_ttl if engine else None,
                depends_on=[POSTGRES_UNIT], labels={"cerebro.io/engine": "lightrag"},
            ))
        return out

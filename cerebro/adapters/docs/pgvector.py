"""pgvector as a retrieval-only DocumentIndex: chunks and their embeddings in the shared Postgres, no extraction.

When to pick it over lightrag:
- The install has no capable chat model endpoint, or cannot afford one extraction pass per chunk at ingest time:
  this adapter only ever calls the embedding endpoint, so a sync costs one embedding request per ~1000 characters
  and nothing more. LightRAG builds a knowledge graph with the LLM (minutes per document on a small GPU).
- The stack should stay small: no engine unit per scope. Everything lives in the `cerebro` database of the shared
  `postgres` unit next to the sync state, so `units()` is empty and there is nothing to provision or idle.
- Answers can be passages rather than prose. `query()` returns the top chunks grouped by document, each under a
  header naming its source; the calling model does the reading. LightRAG synthesises an answer from its graph
  and is the better choice when questions span many documents ("who owns what", "how do X and Y relate").
Pick lightrag when those cross-document answers matter and a model endpoint is available; pick pgvector when
ingest cost, footprint or determinism matter more. Both sit behind the same contract, so the choice is one line
of cerebro.yaml (`engines.docs.type`) and nothing in the gateway or ingest changes.

Storage: one table `docs_chunks(scope, source_id, chunk_no, title, text, embedding vector(dim), updated_at)`,
primary key (scope, source_id, chunk_no), one HNSW index over the embeddings (cosine). Created on first use, as
is the `vector` extension (deploy/postgres/init.sql already creates it for `cerebro`; this is idempotent anyway).
Isolation: every statement carries `WHERE scope = %s`; a row of another scope is never read, counted or deleted.

Connection: options.dsn, else host / port / user / database from options, then POSTGRES_HOST / POSTGRES_PORT /
POSTGRES_USER / POSTGRES_DATABASE (defaults postgres / 5432 / cerebro / cerebro) with the password from the secret
POSTGRES_PASSWORD, exactly as the postgres state adapter. Needs the `pgvector` extra (psycopg 3 + pgvector).

Threading: the ingest engine drives apply() through run_async(), i.e. on a fresh event loop per call, possibly from
a worker thread. Database work therefore uses a *synchronous* psycopg connection opened per operation and run in a
thread (asyncio.to_thread); nothing loop-bound is cached between calls. The embedding call goes through the
Inference adapter on the caller's loop: ctx.inference when the host set one, otherwise an adapter built from
config.inference.type for this operation and closed afterwards.

Options (engines.docs.options): dsn / host / port / user / database, dim (default config.inference.embed.dim),
chunk_size (chars, default 1000), chunk_overlap (default 150), embed_batch (texts per embedding request, default
32), max_distance (cosine distance under which a hit counts as an answer, default 0.6; also opts.extra["max_distance"]),
excerpt_chars (default 300), hnsw_m / hnsw_ef_construction (index build parameters, default pgvector's 16 / 64).
"""
from __future__ import annotations
import asyncio, logging, os, re, threading
from typing import Any, Callable
from cerebro.core import Health, registry
from cerebro.core.contracts.docs import DocumentIndex, Batch, ApplyReport, QueryOptions, Reference, DocAnswer

log = logging.getLogger("cerebro.adapters.docs.pgvector")

TABLE = "docs_chunks"
INDEX = f"{TABLE}_embedding_hnsw"
DEFAULTS = {"host": "postgres", "port": "5432", "user": "cerebro", "database": "cerebro"}
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150
EMBED_BATCH = 32
MAX_DISTANCE = 0.6
EXCERPT_CHARS = 300

_PARAGRAPH = re.compile(r"\n\s*\n")
_BOUNDARIES = (re.compile(r"(?<=[.!?])\s+"), re.compile(r"\n"), re.compile(r" "))   # sentence, line, word


# ================================================================================================= chunking
def _pack(pieces: list[str], size: int, sep: str) -> list[str]:
    """Greedily join consecutive pieces up to `size` characters; a piece over `size` becomes its own part."""
    parts: list[str] = []
    cur = ""
    for piece in pieces:
        cand = f"{cur}{sep}{piece}" if cur else piece
        if len(cand) <= size or not cur:
            cur = cand
        else:
            parts.append(cur); cur = piece
    if cur:
        parts.append(cur)
    return parts


def _split_long(paragraph: str, size: int, level: int = 0) -> list[str]:
    """A paragraph longer than `size`: cut at sentence ends, then lines, then spaces, then hard."""
    if len(paragraph) <= size:
        return [paragraph]
    if level >= len(_BOUNDARIES):
        return [paragraph[i:i + size] for i in range(0, len(paragraph), size)]
    pieces = [p.strip() for p in _BOUNDARIES[level].split(paragraph) if p.strip()]
    out: list[str] = []
    for part in _pack(pieces, size, " "):
        out.extend(_split_long(part, size, level + 1))
    return out


def _tail(text: str, overlap: int) -> str:
    """The last `overlap` characters of a chunk, started at a whitespace boundary, to carry into the next chunk."""
    if overlap <= 0 or len(text) <= overlap:
        return ""
    cut = text[-overlap:]
    ws = cut.find(" ")
    return cut[ws + 1:].strip() if 0 <= ws < len(cut) - 1 else cut.strip()


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """~`size`-character chunks on paragraph boundaries where possible; a paragraph longer than `size` is cut at
    sentence / line / space boundaries. Consecutive chunks share `overlap` characters so a fact split across a
    boundary is still found. Empty input -> no chunks."""
    pieces: list[str] = []
    for p in _PARAGRAPH.split(text.strip()):
        if p.strip():
            pieces.extend(_split_long(p.strip(), size))
    chunks: list[str] = []
    cur = ""
    for piece in pieces:
        cand = f"{cur}\n\n{piece}" if cur else piece
        if len(cand) <= size or not cur:
            cur = cand
            continue
        chunks.append(cur)
        carry = _tail(cur, overlap)
        cur = f"{carry}\n\n{piece}" if carry else piece
    if cur:
        chunks.append(cur)
    return chunks


def header(title: str, source_id: str) -> str:
    """The small header embedded with every chunk so the title's words count, and shown above every passage."""
    return f"# {title}\n({source_id})" if title else f"# {source_id}"


# ================================================================================================= adapter
class Adapter(DocumentIndex):
    name = "pgvector"
    modes = ("vector",)
    default_mode = "vector"

    def __init__(self, options=None, ctx=None):
        super().__init__(options, ctx)
        self._ready = False
        self._ready_lock = threading.Lock()

    # ---------------------------------------------------------------------------------------------- connection
    def dsn(self) -> str:
        """options.dsn wins; otherwise host/port/user/database from options, then env, then defaults."""
        if self.option("dsn"):
            return str(self.option("dsn"))
        env = os.environ
        host = self.option("host") or env.get("POSTGRES_HOST", DEFAULTS["host"])
        port = str(self.option("port") or env.get("POSTGRES_PORT", DEFAULTS["port"]))
        user = self.option("user") or env.get("POSTGRES_USER", DEFAULTS["user"])
        database = self.option("database") or env.get("POSTGRES_DATABASE", DEFAULTS["database"])
        password = (self.ctx.secret("POSTGRES_PASSWORD") if self.ctx else None) or env.get("POSTGRES_PASSWORD")
        parts = [f"host={host}", f"port={port}", f"user={user}", f"dbname={database}"]
        if password:
            parts.append(f"password={password}")
        return " ".join(parts)

    def configured(self) -> bool:
        try:
            import psycopg, pgvector  # noqa: F401
        except ImportError:
            return False
        return bool(self.option("dsn") or self.ctx is None or self.ctx.secret("POSTGRES_PASSWORD") or os.environ.get("POSTGRES_PASSWORD"))

    @property
    def dim(self) -> int:
        d = self.option("dim")
        if d is None and self.ctx is not None:
            d = self.ctx.config.inference.embed.dim
        return int(d or 1024)

    def _ddl(self) -> list[str]:
        m, ef = int(self.option("hnsw_m", 16)), int(self.option("hnsw_ef_construction", 64))
        return [
            "CREATE EXTENSION IF NOT EXISTS vector",
            f"""CREATE TABLE IF NOT EXISTS {TABLE} (
                scope text NOT NULL,
                source_id text NOT NULL,
                chunk_no integer NOT NULL,
                title text NOT NULL DEFAULT '',
                text text NOT NULL,
                embedding vector({self.dim}) NOT NULL,
                updated_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (scope, source_id, chunk_no)
            )""",
            f"CREATE INDEX IF NOT EXISTS {INDEX} ON {TABLE} USING hnsw (embedding vector_cosine_ops) "
            f"WITH (m = {m}, ef_construction = {ef})",
        ]

    def _connect(self):
        """A fresh connection with the vector type registered; the schema is ensured once per adapter."""
        import psycopg
        from pgvector.psycopg import register_vector
        conn = psycopg.connect(self.dsn(), autocommit=False)
        try:
            with self._ready_lock:
                if not self._ready:
                    with conn.cursor() as cur:
                        for stmt in self._ddl():
                            cur.execute(stmt)
                    conn.commit()
                    self._ready = True
            register_vector(conn)
        except Exception:
            conn.close()
            raise
        return conn

    def _run_sync(self, fn: Callable[[Any], Any]) -> Any:
        conn = self._connect()
        try:
            out = fn(conn)
            conn.commit()
            return out
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    async def _run(self, fn: Callable[[Any], Any]) -> Any:
        """fn(conn) in a worker thread on its own connection, committed on success, rolled back on error."""
        return await asyncio.to_thread(self._run_sync, fn)

    # ---------------------------------------------------------------------------------------------- embeddings
    async def _embed(self, texts: list[str]) -> list[list[float]]:
        """Through ctx.inference when the host provides one; else an adapter from config.inference.type built for
        this call (its pooled HTTP client must not outlive the caller's event loop)."""
        if not texts:
            return []
        inf, own = (self.ctx.inference if self.ctx else None), False
        if inf is None:
            if self.ctx is None:
                raise RuntimeError("pgvector adapter needs an AdapterContext with an Inference adapter")
            inf, own = registry.build("inference", self.ctx.config.inference.type, {}, self.ctx), True
        batch = max(1, int(self.option("embed_batch", EMBED_BATCH)))
        out: list[list[float]] = []
        try:
            for i in range(0, len(texts), batch):
                vecs = await inf.embed(texts[i:i + batch])
                if len(vecs) != len(texts[i:i + batch]):
                    raise RuntimeError(f"embedding endpoint returned {len(vecs)} vectors for {len(texts[i:i + batch])} texts")
                out.extend(vecs)
        finally:
            if own and hasattr(inf, "aclose"):
                await inf.aclose()
        for v in out:
            if len(v) != self.dim:
                raise RuntimeError(f"embedding has {len(v)} dimensions, table expects {self.dim} (inference.embed.dim / options.dim)")
        return out

    # ---------------------------------------------------------------------------------------------- contract
    async def apply(self, scope: str, batch: Batch) -> ApplyReport:
        """Embed every chunk first (nothing touched if the endpoint fails), then delete + insert in ONE
        transaction so a failed batch leaves the scope as it was and the caller's sync state untouched."""
        if batch.scope != scope:
            raise ValueError(f"batch is for scope '{batch.scope}', not '{scope}'")
        if batch.empty:
            return ApplyReport(scope=scope)
        from pgvector import Vector
        size, overlap = int(self.option("chunk_size", CHUNK_SIZE)), int(self.option("chunk_overlap", CHUNK_OVERLAP))
        rows: list[tuple] = []                       # (source_id, chunk_no, title, text)
        for d in batch.upserts:
            for n, body in enumerate(chunk_text(d.text, size, overlap)):
                rows.append((d.source_id, n, d.title, body))
        vectors = await self._embed([f"{header(t, s)}\n\n{body}" for s, _, t, body in rows])
        deletes = sorted(batch.deletes)

        def write(conn) -> tuple[int, int]:
            with conn.cursor() as cur:
                deleted = 0
                if deletes:
                    cur.execute(f"SELECT count(DISTINCT source_id) FROM {TABLE} WHERE scope = %s AND source_id = ANY(%s)",
                                (scope, deletes))
                    deleted = int(cur.fetchone()[0])
                    cur.execute(f"DELETE FROM {TABLE} WHERE scope = %s AND source_id = ANY(%s)", (scope, deletes))
                if rows:
                    cur.executemany(
                        f"INSERT INTO {TABLE} (scope, source_id, chunk_no, title, text, embedding, updated_at) "
                        f"VALUES (%s, %s, %s, %s, %s, %s, now()) "
                        f"ON CONFLICT (scope, source_id, chunk_no) DO UPDATE SET title = EXCLUDED.title, "
                        f"text = EXCLUDED.text, embedding = EXCLUDED.embedding, updated_at = now()",
                        [(scope, s, n, t, body, Vector(v)) for (s, n, t, body), v in zip(rows, vectors)])
            return deleted, len(rows)

        deleted, chunks = await self._run(write)
        inserted = len({s for s, *_ in rows})
        log.info("%s: deleted %d documents, inserted %d documents (%d chunks)", scope, deleted, inserted, chunks)
        return ApplyReport(scope=scope, deleted=deleted, inserted=inserted,
                           note=f"{chunks} chunks embedded" if rows else None)

    async def query(self, scope: str, query: str, opts: QueryOptions | None = None) -> DocAnswer:
        """Cosine top_k within the scope, grouped by document. `answered` is True when at least one chunk is
        closer than max_distance (options / opts.extra), so the gateway can fall back to live sources otherwise."""
        opts = opts or QueryOptions()
        self.check_mode(opts.mode)
        from pgvector import Vector
        threshold = float(opts.extra.get("max_distance", self.option("max_distance", MAX_DISTANCE)))
        excerpt_chars = int(self.option("excerpt_chars", EXCERPT_CHARS))
        top_k = max(1, int(opts.top_k))
        (qvec,) = await self._embed([query])

        def search(conn) -> list[tuple]:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT source_id, chunk_no, title, text, embedding <=> %s AS distance FROM {TABLE} "
                    f"WHERE scope = %s ORDER BY distance, source_id, chunk_no LIMIT %s",
                    (Vector(qvec), scope, top_k))
                return cur.fetchall()

        hits = await self._run(search)
        by_source: dict[str, dict[str, Any]] = {}          # insertion order = best distance first (hits are sorted)
        for source_id, chunk_no, title, text, _distance in hits:
            d = by_source.setdefault(source_id, {"title": title, "excerpt": text, "chunks": {}})
            d["chunks"][int(chunk_no)] = text
        sections, refs = [], []
        for source_id, d in by_source.items():
            passages = [d["chunks"][n] for n in sorted(d["chunks"])]
            sections.append(f"## {d['title'] or source_id}\nsource: {source_id}\n\n" + "\n\n[...]\n\n".join(passages))
            excerpt = d["excerpt"]
            refs.append(Reference(id=str(len(refs) + 1), source=source_id, title=d["title"] or None,
                                  excerpt=excerpt[:excerpt_chars] + ("..." if len(excerpt) > excerpt_chars else "")))
        answered = any(float(h[4]) < threshold for h in hits)
        raw = {"mode": "vector", "top_k": top_k, "max_distance": threshold,
               "hits": [{"source_id": s, "chunk_no": int(n), "distance": float(dist)} for s, n, _, _, dist in hits]}
        return DocAnswer(scope=scope, answer="\n\n".join(sections), answered=answered, references=refs, raw=raw)

    async def health(self, scope: str) -> Health:
        def probe(conn) -> dict:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                row = cur.fetchone()
                cur.execute("SELECT to_regclass(%s) IS NOT NULL", (TABLE,))
                table = bool(cur.fetchone()[0])
            return {"vector": row[0] if row else None, "table": table}
        try:
            info = await self._run(probe)
        except Exception as e:                        # psycopg errors, missing driver, bad DSN
            return Health.down(f"pgvector ({scope}): {type(e).__name__}: {e}".rstrip(": "))
        if not info["vector"]:
            return Health.down(f"pgvector ({scope}): extension 'vector' not installed", table=info["table"])
        return Health.up(f"pgvector {info['vector']} in {self.dsn().split('password=')[0].strip()}",
                         vector=info["vector"], table=info["table"], dim=self.dim)

    async def stats(self, scope: str) -> dict:
        def count(conn) -> tuple[int, int]:
            with conn.cursor() as cur:
                cur.execute(f"SELECT count(DISTINCT source_id), count(*) FROM {TABLE} WHERE scope = %s", (scope,))
                s, c = cur.fetchone()
            return int(s), int(c)
        sources, chunks = await self._run(count)
        return {"sources": sources, "chunks": chunks, "dim": self.dim}

    # no engine unit: the data lives in the shared postgres unit (database `cerebro`), which the stack provisions anyway
    def units(self):
        return []

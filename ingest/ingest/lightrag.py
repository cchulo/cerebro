"""LightRAG server client, one instance per scope. Verified against ghcr.io/hkuds/lightrag:v1.5.7.

Facts that shape this client:
- POST /documents/texts {texts, file_sources}: a file_source is the document's identity (dedup key, doc_id seed).
  LightRAG keeps only its basename (Path(file_source).name), so a source id must not contain "/": we encode "/" as "|".
- Inserting a file_source that already exists is refused with 409. There is no in-place update: delete, then insert.
- DELETE /documents/delete_document {doc_ids} runs in the background and answers status="busy" while the pipeline is
  processing anything. So a sync is done as a Batch: list -> delete (pipeline idle) -> insert everything at once.
- There is no GET /documents; POST /documents/paginated lists documents (id, file_path, status).
"""
import logging, time
import httpx
from .config import LIGHTRAG_API_KEY
from . import scopes

log = logging.getLogger("ingest.lightrag")
_headers = {"X-API-Key": LIGHTRAG_API_KEY} if LIGHTRAG_API_KEY else {}
_clients: dict[str, httpx.Client] = {}
INSERT_CHUNK = 25          # texts per /documents/texts request (limit is 50 MiB per request)
IDLE_TIMEOUT = 3600        # seconds to wait for the pipeline before giving up on deletes

def encode_source(source_id: str) -> str:
    return source_id.replace("/", "|")

def decode_source(file_path: str) -> str:
    return file_path.replace("|", "/")

def _c(scope: str) -> httpx.Client:
    if scope not in _clients:
        _clients[scope] = httpx.Client(base_url=scopes.lightrag_url(scope), headers=_headers, timeout=300)
    return _clients[scope]

def pipeline_status(scope: str) -> dict:
    r = _c(scope).get("/documents/pipeline_status"); r.raise_for_status()
    return r.json()

def wait_idle(scope: str, timeout: float = IDLE_TIMEOUT, poll: float = 3.0, full: bool = True) -> None:
    """full=True: wait until nothing is processing (required before a delete).
    full=False: wait only for the short enqueue window, so a listing sees documents inserted moments ago."""
    deadline = time.monotonic() + timeout
    while True:
        st = pipeline_status(scope)
        blocking = st.get("destructive_busy") or st.get("scanning") or st.get("pending_enqueues")
        if full:
            blocking = blocking or st.get("busy")
        if not blocking:
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"lightrag-{scope} pipeline still busy after {timeout}s: {st.get('latest_message')}")
        time.sleep(poll)

def list_documents(scope: str) -> dict[str, list[str]]:
    """decoded source id -> LightRAG doc ids (any status)."""
    out: dict[str, list[str]] = {}
    page = 1
    while True:
        r = _c(scope).post("/documents/paginated", json={"page": page, "page_size": 200})
        r.raise_for_status()
        body = r.json()
        for d in body.get("documents", []):
            out.setdefault(decode_source(d["file_path"]), []).append(d["id"])
        if not body.get("pagination", {}).get("has_next"):
            return out
        page += 1

def delete_ids(scope: str, doc_ids: list[str]) -> None:
    if not doc_ids:
        return
    wait_idle(scope)
    for _ in range(200):
        r = _c(scope).request("DELETE", "/documents/delete_document",
                              json={"doc_ids": doc_ids, "delete_file": False, "delete_llm_cache": False})
        r.raise_for_status()
        status = r.json().get("status")
        if status == "deletion_started":
            wait_idle(scope)
            return
        if status == "busy":
            time.sleep(3); continue
        raise RuntimeError(f"lightrag-{scope} refused delete: {r.json()}")
    raise TimeoutError(f"lightrag-{scope}: could not acquire the pipeline to delete {len(doc_ids)} documents")

class Batch:
    """Collect changes for one scope, then apply them in the order LightRAG needs (deletes first, inserts together)."""
    def __init__(self, scope: str):
        self.scope = scope
        self.deletes: set[str] = set()
        self.inserts: list[tuple[str, str]] = []

    def delete(self, source_id: str) -> None:
        self.deletes.add(source_id)

    def upsert(self, source_id: str, text: str, title: str = "") -> None:
        self.deletes.add(source_id)                      # remove any previous version first
        self.inserts.append((source_id, f"# {title}\n\n{text}" if title else text))

    def flush(self) -> dict:
        deleted = inserted = 0
        if self.deletes:
            wait_idle(self.scope, full=False)           # documents enqueued moments ago may not be listed yet
            index = list_documents(self.scope)
            ids = [i for s in self.deletes for i in index.get(s, [])]
            if ids:                                     # only a real deletion needs the pipeline fully idle
                log.info("lightrag-%s: deleting %d documents", self.scope, len(ids))
                delete_ids(self.scope, ids); deleted = len(ids)
        for i in range(0, len(self.inserts), INSERT_CHUNK):
            chunk = self.inserts[i:i + INSERT_CHUNK]
            r = _c(self.scope).post("/documents/texts", json={
                "texts": [t for _, t in chunk], "file_sources": [encode_source(s) for s, _ in chunk]})
            if r.status_code == 409:
                raise RuntimeError(f"lightrag-{self.scope}: {r.json().get('detail')}")
            r.raise_for_status()
            inserted += len(chunk)
        log.info("lightrag-%s: deleted %d, inserted %d (processing continues in the background)",
                 self.scope, deleted, inserted)
        self.deletes.clear(); self.inserts.clear()
        return {"deleted": deleted, "inserted": inserted}

class Batches:
    """One Batch per scope, created on demand."""
    def __init__(self):
        self._b: dict[str, Batch] = {}
    def __getitem__(self, scope: str) -> Batch:
        return self._b.setdefault(scope, Batch(scope))
    def flush(self) -> dict:
        return {s: b.flush() for s, b in self._b.items()}

def health() -> dict:
    out = {}
    for s in scopes.SCOPES:
        try:
            out[s] = _c(s).get("/health", timeout=5).status_code == 200
        except httpx.HTTPError:
            out[s] = False
    return out

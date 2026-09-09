"""Minimal LightRAG server client, one instance per scope.
   POST /documents/text  {text, file_source}    DELETE /documents/delete_document {doc_ids}
Check GET {url}/docs on your image version if a path differs."""
import httpx
from .config import LIGHTRAG_API_KEY
from . import scopes

_headers = {"X-API-Key": LIGHTRAG_API_KEY} if LIGHTRAG_API_KEY else {}
_clients: dict[str, httpx.Client] = {}

def _c(scope: str) -> httpx.Client:
    if scope not in _clients:
        _clients[scope] = httpx.Client(base_url=scopes.lightrag_url(scope), headers=_headers, timeout=120)
    return _clients[scope]

def upsert_text(scope: str, source_id: str, text: str, title: str = "") -> None:
    body = f"# {title}\n\n{text}" if title else text
    _c(scope).post("/documents/text", json={"text": body, "file_source": source_id}).raise_for_status()

def delete_by_source(scope: str, source_id: str) -> None:
    r = _c(scope).get("/documents"); r.raise_for_status()
    for group in r.json().get("statuses", {}).values():
        for doc in group:
            if doc.get("file_path") == source_id:
                _c(scope).request("DELETE", "/documents/delete_document",
                                  json={"doc_ids": [doc["id"]], "delete_file": False})

def health() -> dict:
    out = {}
    for s in scopes.SCOPES:
        try:
            out[s] = _c(s).get("/health").status_code == 200
        except httpx.HTTPError:
            out[s] = False
    return out

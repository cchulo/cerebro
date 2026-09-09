"""Tiny JSON state store: remembers the last-seen version of every source item
so we only re-ingest what changed (LightRAG updates incrementally)."""
import json, os, threading
from .config import STATE_DIR

_lock = threading.Lock()
_path = os.path.join(STATE_DIR, "versions.json")

def _load() -> dict:
    try:
        with open(_path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def get(key: str) -> str | None:
    with _lock:
        return _load().get(key)

def set(key: str, version: str) -> None:
    with _lock:
        d = _load(); d[key] = version
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(_path, "w") as f:
            json.dump(d, f)

def keys_with_prefix(prefix: str) -> list[str]:
    with _lock:
        return [k for k in _load() if k.startswith(prefix)]

def delete(key: str) -> None:
    with _lock:
        d = _load(); d.pop(key, None)
        with open(_path, "w") as f:
            json.dump(d, f)

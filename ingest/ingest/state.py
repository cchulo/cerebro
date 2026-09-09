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

def _save(d: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = _path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, _path)

def get(key: str) -> str | None:
    with _lock:
        return _load().get(key)

def keys_with_prefix(prefix: str) -> list[str]:
    with _lock:
        return [k for k in _load() if k.startswith(prefix)]

def commit(set_items: dict[str, str], delete_keys: set[str] | list[str] = ()) -> None:
    """Apply a sync's outcome atomically, after the LightRAG batch was accepted."""
    with _lock:
        d = _load()
        for k in delete_keys:
            d.pop(k, None)
        d.update(set_items)
        _save(d)

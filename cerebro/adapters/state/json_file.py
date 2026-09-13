"""SyncState in one JSON file (the v1 ingest/state.py). One ingest replica only: fine for compose and laptops.

Options: path (the file) or dir (directory holding versions.json); default $CEREBRO_STATE_DIR or ./state.
"""
from __future__ import annotations
import json, os, threading
from cerebro.core.contracts.state import SyncState

DEFAULT_DIR = "./state"
FILE_NAME = "versions.json"


class Adapter(SyncState):
    name = "json_file"

    def __init__(self, options=None, ctx=None):
        super().__init__(options, ctx)
        path = self.option("path")
        if not path:
            directory = self.option("dir") or os.environ.get("CEREBRO_STATE_DIR", DEFAULT_DIR)
            path = os.path.join(directory, FILE_NAME)
        self.path = str(path)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            with open(self.path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _save(self, d: dict) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, self.path)              # atomic on POSIX: readers never see a half-written file

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._load().get(key)

    def keys_with_prefix(self, prefix: str) -> list[str]:
        with self._lock:
            return [k for k in self._load() if k.startswith(prefix)]

    def commit(self, set_items: dict[str, str], delete_keys: set[str] | list[str] = ()) -> None:
        with self._lock:
            d = self._load()
            for k in delete_keys:
                d.pop(k, None)
            d.update(set_items)
            self._save(d)

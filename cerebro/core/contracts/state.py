"""SyncState: last-seen version of every source item, so the ingest re-ingests only what changed.
Keys are "<plugin>:<scope>:<doc.key>" and double as DocumentIndex source ids."""
from __future__ import annotations
from abc import abstractmethod
from ..context import Adapter


class SyncState(Adapter):
    kind = "state"

    @abstractmethod
    def get(self, key: str) -> str | None: ...

    @abstractmethod
    def keys_with_prefix(self, prefix: str) -> list[str]: ...

    @abstractmethod
    def commit(self, set_items: dict[str, str], delete_keys: set[str] | list[str] = ()) -> None:
        """Apply a sync's outcome atomically, after the index accepted the batch."""

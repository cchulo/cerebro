"""cerebro.ingest: the sync engine. Reads the source plugins, diffs against SyncState, hands one Batch per scope
to the DocumentIndex adapter. Imports cerebro.core only; engines arrive through cerebro.core.registry."""
from .runtime import Ingest
from .sync import sync

__all__ = ["Ingest", "sync"]

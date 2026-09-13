"""Sync engine: for every (scope, source plugin) pair, diff the plugin's documents against the last-seen versions,
collect the changes into ONE Batch per scope, hand it to the DocumentIndex adapter, reconcile deletions, then commit
state. The adapter decides ordering and idle waits; if it raises, state is left untouched and the next run retries.

State keys are "<plugin>:<scope>:<doc.key>" and double as the index's source ids, so a document lives in exactly
one scope's index and can be located again for deletion.
"""
from __future__ import annotations
import logging
from cerebro.core.contracts.docs import Batch
from .runtime import Ingest, run_async

log = logging.getLogger("cerebro.ingest.sync")


def sync(ingest: Ingest, source_name: str | None = None, filter: dict | None = None,
         only_scopes: list[str] | None = None) -> dict:
    """Returns {"<scope>/<source>": {"changed", "removed"} | {"skipped"}, "<scope>": {"index": ApplyReport}}.
    `filter` is a plugin-specific webhook filter; keys outside what the plugin `covers()` are never deleted."""
    report: dict = {}
    state, index = ingest.state, ingest.index
    for scope in ingest.scopes():
        if only_scopes and scope not in only_scopes:
            continue
        batch, new_state, drop = Batch(scope=scope), {}, set()
        for name, cfg in ingest.docs_config(scope).items():
            if source_name and name != source_name:
                continue
            src = ingest.sources.load(name)
            if not src.configured():
                report[f"{scope}/{name}"] = {"skipped": f"{name} not configured"}
                continue
            ctx = ingest.scope_context(scope, name)
            prefix = f"{name}:{scope}:"
            seen, changed, removed = set(), 0, 0
            for doc in src.documents(ctx, filter):
                key = prefix + doc.key
                seen.add(key)
                if state.get(key) == doc.version:
                    continue
                batch.upsert(key, doc.text, title=doc.title)
                new_state[key] = doc.version
                changed += 1
            for key in state.keys_with_prefix(prefix):
                if key not in seen and src.covers(key[len(prefix):], filter):
                    batch.delete(key); drop.add(key); removed += 1
            report[f"{scope}/{name}"] = {"changed": changed, "removed": removed}
        if not batch.empty:
            applied = run_async(index.apply(scope, batch))          # raises before state is touched if refused
            state.commit(new_state, drop)
            report[scope] = {"index": applied.model_dump(exclude_none=True)}
        log.info("sync %s: %s", scope, {k: v for k, v in report.items() if k == scope or k.startswith(scope + "/")})
    return report

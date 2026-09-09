"""Sync engine: for every (scope, adapter) pair, diff the adapter's documents against the last-seen versions,
batch the changes into the scope's LightRAG instance, reconcile deletions, then commit state.

State keys are "<adapter>:<scope>:<doc.key>" and double as the LightRAG source id (with "/" encoded by
lightrag.py), so a document lives in exactly one scope's index and can be located again for deletion.
"""
import logging
from . import scopes, state, lightrag, sources
from .sources import ScopeContext

log = logging.getLogger("ingest.sync")


def sync(source_name: str | None = None, filter: dict | None = None, only_scopes: list[str] | None = None) -> dict:
    """One Batch per scope, filled by every adapter of that scope, flushed once; state committed after the flush."""
    report: dict = {}
    for scope, sc in scopes.SCOPES.items():
        if only_scopes and scope not in only_scopes:
            continue
        batch, new_state, drop = lightrag.Batch(scope), {}, set()
        for name, cfg in scopes.docs_config(scope).items():
            if source_name and name != source_name:
                continue
            src = sources.load(name, scopes.SOURCES.get(name))
            if not src.configured():
                report[f"{scope}/{name}"] = {"skipped": f"{name} not configured"}
                continue
            ctx = ScopeContext(scope=scope, config=cfg, repos=sc.get("repos", []))
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
        if batch.deletes or batch.inserts:
            flushed = batch.flush()                    # raises before state is touched if LightRAG refused
            state.commit(new_state, drop)
            report[scope] = {"lightrag": flushed}
        log.info("sync %s: %s", scope, {k: v for k, v in report.items() if k == scope or k.startswith(scope + "/")})
    return report

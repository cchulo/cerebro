"""Wiring for the ingest engine: config -> DocumentIndex adapter, SyncState adapter, source plugins, Locator.

    ingest = Ingest.from_config(load_config("cerebro.yaml"))
    sync(ingest)                               # or create_app(ingest) for the service

The docs adapter comes from engines.docs.type, the state adapter from gateway.state_type (it lives under the
gateway block for now). The Locator is the provisioner adapter for provisioning.target when that module imports;
otherwise a StaticLocator whose table is filled from the ports the docs adapter's own UnitSpecs declare
(http://<unit>:<port>, which is what compose service names resolve to) and whose fallback template is
$CEREBRO_UNIT_URL_TEMPLATE or http://{unit}:8080.
"""
from __future__ import annotations
import asyncio, concurrent.futures, importlib, logging, os
from typing import Any, Coroutine
from cerebro.core import AdapterContext, StaticLocator, Locator, Secrets, registry
from cerebro.core.config import Config
from cerebro.core.contracts.docs import DocumentIndex
from cerebro.core.contracts.sources import ScopeContext
from cerebro.core.contracts.state import SyncState
from .plugins import SourceRegistry

log = logging.getLogger("cerebro.ingest")
DEFAULT_UNIT_URL_TEMPLATE = "http://{unit}:8080"


def run_async(coro: Coroutine) -> Any:
    """Run a coroutine from synchronous code (the sync engine, a scheduler thread). If this thread already runs an
    event loop, the coroutine runs on a fresh loop in a helper thread instead of deadlocking the caller's."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


def build_locator(config: Config, secrets: Secrets | None = None) -> Locator:
    target = config.provisioning.target
    try:
        importlib.import_module(f"cerebro.adapters.provision.{target}")
    except ImportError as e:
        log.warning("provisioner '%s' not importable (%s); unit URLs from a static template", target, e)
        return StaticLocator(template=os.environ.get("CEREBRO_UNIT_URL_TEMPLATE", DEFAULT_UNIT_URL_TEMPLATE))
    boot = AdapterContext(config, secrets=secrets)
    return registry.build("provision", target, config.provisioning.options, boot)   # type: ignore[return-value]


class Ingest:
    """Everything a sync needs, built once per process."""

    def __init__(self, config: Config, *, index: DocumentIndex, state: SyncState, sources: SourceRegistry,
                 ctx: AdapterContext | None = None):
        self.config, self.index, self.state, self.sources, self.ctx = config, index, state, sources, ctx

    @classmethod
    def from_config(cls, config: Config, *, secrets: Secrets | None = None, locator: Locator | None = None,
                    plugins_dir: str | None = None) -> "Ingest":
        docs = config.engines.docs
        if docs is None:
            raise RuntimeError("cerebro.yaml has no engines.docs; nothing to ingest into")
        locator = locator or build_locator(config, secrets)
        ctx = AdapterContext(config, secrets=secrets, locator=locator)
        index = registry.build("docs", docs.type, docs.options, ctx)
        state = registry.build("state", config.gateway.state_type, {}, ctx)
        if isinstance(locator, StaticLocator):
            for u in index.units():
                locator.urls.setdefault(u.name, f"http://{u.name}:{u.http_port}")
        return cls(config, index=index, state=state, sources=SourceRegistry(config, plugins_dir), ctx=ctx)   # type: ignore[arg-type]

    # ---- config views the engine needs
    def scopes(self) -> list[str]:
        return list(self.config.scopes)

    def docs_config(self, scope: str) -> dict[str, dict]:
        return dict(self.config.scopes[scope].docs)

    def repos(self, scope: str) -> list[str]:
        return [r.url for r in self.config.scopes[scope].code.repos]

    def source_names(self) -> list[str]:
        return self.config.source_names()

    def scopes_using(self, source: str) -> list[str]:
        return [s for s in self.config.scopes if source in self.config.scopes[s].docs]

    def scope_context(self, scope: str, source: str) -> ScopeContext:
        return ScopeContext(scope=scope, config=self.docs_config(scope).get(source) or {}, repos=self.repos(scope))

    def secret(self, name: str, default: str | None = None) -> str | None:
        if self.ctx is not None:
            return self.ctx.secret(name, default)
        return os.environ.get(name, default)

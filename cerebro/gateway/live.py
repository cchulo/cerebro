"""Gateway side of the source plugins: the `live` part of every plugin in gateway.plugins_dir.

A plugin that ships a live part is enabled as a fallback by default. Per-plugin overrides live in cerebro.yaml
under `sources.<plugin>.live` (v1 had a separate top-level `live:` map):

    sources:
      confluence:
        rest_url: https://wiki.internal            # the plugin's own options, shared with ingest
        live: { enabled: true, fallback: true, via: mcp, url: http://mcp-confluence:9000/mcp }

`enabled: false` hides the plugin from live_search/live_fetch; `fallback: false` keeps it out of query_docs'
automatic fallback but still callable; everything else in `live` is passed to the LiveSource with the plugin's
options. `allowed` for a call is built from the caller's Grants: one entry per allowed scope that lists the plugin
under `docs:`, carrying that scope's entry (spaces, projects, paths) so the source can confine itself.
"""
from __future__ import annotations
import importlib
import logging
from cerebro.core import Config, Grants
from cerebro.core.contracts import LiveSource, Plugin, discover
from cerebro.core.types import Forbidden

log = logging.getLogger("cerebro.gateway.live")
_LIVE_ONLY_KEYS = ("enabled", "fallback")


class LiveRegistry:
    def __init__(self, config: Config, plugins: dict[str, Plugin] | None = None):
        self.config = config
        self._plugins = plugins
        self._instances: dict[str, LiveSource] = {}

    @property
    def plugins(self) -> dict[str, Plugin]:
        """name -> Plugin, live-capable ones only. Discovered from gateway.plugins_dir on first use."""
        if self._plugins is None:
            self._plugins = {n: p for n, p in discover(self.config.gateway.plugins_dir).items() if p.live is not None}
        return self._plugins

    def overrides(self, name: str) -> dict:
        return dict((self.config.sources.get(name) or {}).get("live") or {})

    def enabled(self) -> dict[str, dict]:
        """name -> live overrides for every live-capable plugin not switched off."""
        out: dict[str, dict] = {}
        for name in self.plugins:
            opts = self.overrides(name)
            if opts.get("enabled", True) is not False:
                out[name] = opts
        return out

    def fallbacks(self) -> list[str]:
        return [n for n, o in self.enabled().items() if o.get("fallback", True) is not False]

    def load(self, name: str) -> LiveSource:
        """The LiveSource instance for an enabled plugin; Forbidden when unknown or disabled, RuntimeError when
        the plugin says it lacks credentials or an upstream."""
        if name in self._instances:
            return self._instances[name]
        on = self.enabled()
        if name not in on:
            raise Forbidden(f"no enabled live source named '{name}'; enabled: {sorted(on)}")
        options = {k: v for k, v in (self.config.sources.get(name) or {}).items() if k != "live"}
        options.update({k: v for k, v in on[name].items() if k not in _LIVE_ONLY_KEYS})
        type_ = options.pop("type", None)
        if type_ and ":" in str(type_):
            mod, cls = str(type_).split(":", 1)
            klass = getattr(importlib.import_module(mod), cls)
        else:
            klass = self.plugins[name].live
        inst = klass(options)
        inst.name = name
        if not inst.configured():
            raise RuntimeError(f"live source '{name}' is not configured (missing credentials or upstream url)")
        self._instances[name] = inst
        return inst

    def allowed(self, grants: Grants, source: str, scopes: list[str] | None = None) -> list[dict]:
        """Per allowed scope, the scope's `docs:` entry for `source` (Forbidden for a scope outside the grants)."""
        out: list[dict] = []
        for s in grants.check_scopes(scopes):
            cfg = self.config.scopes[s].docs.get(source) if s in self.config.scopes else None
            if cfg is not None:
                out.append({"scope": s, **cfg})
        return out

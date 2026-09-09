"""Gateway-side plugin registry: the `live` part of every plugin found by stack_plugins.discover(PLUGINS_DIR).
A plugin that ships a live part is enabled as a fallback by default; the optional top-level `live:` map in
config/scopes.yaml only carries per-plugin overrides: { enabled: false } | { fallback: false } | { via: rest } |
{ url: ..., auth_env: ... } | tool/argument names.
"""
import importlib, logging
import stack_plugins
from stack_plugins import LiveSource, Plugin

log = logging.getLogger("gateway.live")
PLUGINS: dict[str, Plugin] = {}
_instances: dict[str, LiveSource] = {}


def discover() -> None:
    PLUGINS.update({n: p for n, p in stack_plugins.discover().items() if p.live is not None})


def enabled(overrides: dict[str, dict]) -> dict[str, dict]:
    """name -> options for every live-capable plugin not switched off. `overrides` = the `live:` map."""
    if not PLUGINS:
        discover()
    out = {}
    for name in PLUGINS:
        opts = dict(overrides.get(name) or {})
        if opts.pop("enabled", True) is not False:
            out[name] = opts
    return out


def load(name: str, options: dict | None = None) -> LiveSource:
    if name in _instances:
        return _instances[name]
    if not PLUGINS:
        discover()
    spec = dict(options or {}); spec.pop("enabled", None); spec.pop("fallback", None)
    type_ = spec.pop("type", None)
    if type_ and ":" in type_:
        mod, cls = type_.split(":", 1); klass = getattr(importlib.import_module(mod), cls)
    elif name in PLUGINS:
        klass = PLUGINS[name].live
    else:
        raise KeyError(f"no plugin with a live part named '{name}'; available: {sorted(PLUGINS)}")
    inst = klass(spec); inst.name = name
    _instances[name] = inst
    return inst


__all__ = ["LiveSource", "PLUGINS", "discover", "enabled", "load"]

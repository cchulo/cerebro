"""Ingest-side plugin registry: the `source` part of every plugin found by stack_plugins.discover(PLUGINS_DIR)
(default /plugins), plus installed packages exposing a `context_stack.sources` entry point. Nothing is built in.
A scope references a plugin by `name` under `docs:`; the optional top-level `sources:` map carries non-secret options.
"""
import importlib, importlib.metadata, logging
import stack_plugins
from stack_plugins import Source, Document, ScopeContext

log = logging.getLogger("ingest.sources")
REGISTRY: dict[str, type[Source]] = {}
ORIGIN: dict[str, str] = {}
_instances: dict[str, Source] = {}


def discover() -> None:
    for name, plugin in stack_plugins.discover().items():
        if plugin.source is not None:
            REGISTRY[name] = plugin.source; ORIGIN[name] = f"plugin {plugin.source.__module__.split('.')[-1]}.py"
    try:
        eps = importlib.metadata.entry_points(group="context_stack.sources")
    except TypeError:
        eps = importlib.metadata.entry_points().get("context_stack.sources", [])
    for ep in eps:
        try:
            cls = ep.load()
            if not getattr(cls, "name", None) or cls.name == "base":
                cls.name = ep.name
            REGISTRY[cls.name] = cls; ORIGIN[cls.name] = f"entry point {ep.value}"
        except Exception:
            log.exception("entry point %s failed to load; skipped", ep.name)


def load(name: str, declared: dict | None = None) -> Source:
    """Instantiate the source part of plugin `name` (cached). `declared` = its `sources:` options, if any."""
    if name in _instances:
        return _instances[name]
    if not REGISTRY:
        discover()
    spec = dict(declared or {})
    type_ = spec.pop("type", name)
    if ":" in type_:
        mod, cls = type_.split(":", 1)
        klass = getattr(importlib.import_module(mod), cls)
    elif type_ in REGISTRY:
        klass = REGISTRY[type_]
    else:
        raise KeyError(f"no plugin with an ingest part named '{name}'; available: {sorted(REGISTRY)}. "
                       f"Add plugins/{name}.py (docs/PLUGINS.md) or a package with a context_stack.sources entry point.")
    inst = klass(spec); inst.name = name
    _instances[name] = inst
    return inst


def available() -> dict[str, str]:
    if not REGISTRY:
        discover()
    return dict(ORIGIN)


__all__ = ["Source", "Document", "ScopeContext", "REGISTRY", "discover", "load", "available"]

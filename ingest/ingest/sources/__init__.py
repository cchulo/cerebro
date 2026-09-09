"""Adapter registry. The engine ships no privileged sources: confluence, backstage, git and files are plugin files
like any other (plugins/sources/). Two discovery paths, so adding a source never means editing this package:

1. plugin files: every ``*.py`` in ``INGEST_PLUGINS_DIR`` (default ``/plugins/sources``) is imported and each
   ``Source`` subclass with a ``name`` is registered;
2. installed packages that declare an entry point in group ``context_stack.sources``
   (``[project.entry-points."context_stack.sources"] jama = "my_pkg.jama:JamaSource"``).

A scope then just references the adapter by name under ``docs:``. The optional top-level ``sources:`` map in
scopes.yaml is only for non-secret options (``jama: { url: https://jama.internal }``) or to point a name at an
explicit class (``type: "pkg.mod:Class"``).
"""
import importlib, importlib.metadata, importlib.util, inspect, logging, os, pathlib, sys
from .base import Source, Document, ScopeContext

log = logging.getLogger("ingest.sources")
REGISTRY: dict[str, type[Source]] = {}
ORIGIN: dict[str, str] = {}
_instances: dict[str, Source] = {}


def register(cls: type[Source], origin: str = "builtin") -> type[Source]:
    if not getattr(cls, "name", None) or cls.name == "base":
        raise ValueError(f"{cls.__name__} must set a `name`")
    if cls.name in REGISTRY and REGISTRY[cls.name] is not cls:
        log.warning("source '%s' from %s overrides the one from %s", cls.name, origin, ORIGIN.get(cls.name))
    REGISTRY[cls.name] = cls
    ORIGIN[cls.name] = origin
    return cls


def _register_module(mod, origin: str) -> int:
    n = 0
    for _, obj in inspect.getmembers(mod, inspect.isclass):
        if issubclass(obj, Source) and obj is not Source and obj.__module__ == mod.__name__ and getattr(obj, "name", "base") != "base":
            register(obj, origin); n += 1
    return n


def discover() -> None:
    """Load plugin files and entry points. Safe to call more than once."""
    plugins = pathlib.Path(os.environ.get("INGEST_PLUGINS_DIR", "/plugins/sources"))
    if plugins.is_dir():
        if str(plugins) not in sys.path:
            sys.path.insert(0, str(plugins))            # plugins may import sibling helper modules
        for f in sorted(plugins.glob("*.py")):
            if f.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(f"plugins.{f.stem}", f)
                mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
                n = _register_module(mod, f"plugin {f.name}")
                log.info("plugin %s: %d source(s)", f.name, n)
            except Exception:
                log.exception("plugin %s failed to load; skipped", f.name)
    try:
        eps = importlib.metadata.entry_points(group="context_stack.sources")
    except TypeError:                                   # python < 3.10 signature
        eps = importlib.metadata.entry_points().get("context_stack.sources", [])
    for ep in eps:
        try:
            cls = ep.load()
            if not getattr(cls, "name", None):
                cls.name = ep.name
            register(cls, f"entry point {ep.value}")
        except Exception:
            log.exception("entry point %s failed to load; skipped", ep.name)


def load(name: str, declared: dict | None = None) -> Source:
    """Instantiate adapter `name` (cached). `declared` is its `sources:` entry, if any."""
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
        raise KeyError(f"unknown source '{name}' (type '{type_}'); available: {sorted(REGISTRY)}. "
                       f"Drop a plugin into {os.environ.get('INGEST_PLUGINS_DIR', '/plugins/sources')} or install a package "
                       f"with a context_stack.sources entry point.")
    inst = klass(spec)
    inst.name = name
    _instances[name] = inst
    return inst


def available() -> dict[str, str]:
    if not REGISTRY:
        discover()
    return dict(ORIGIN)


__all__ = ["Source", "Document", "ScopeContext", "REGISTRY", "register", "discover", "load", "available"]

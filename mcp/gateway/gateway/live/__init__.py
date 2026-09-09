"""Registry for live sources: every *.py in GATEWAY_PLUGINS_DIR (default /plugins/live); nothing is built in.
Enabled per deployment by the top-level `live:` map in config/scopes.yaml:
    live:
      confluence: {}                       # plugins/live/confluence.py, credentials from env
      jama: { url: https://jama.internal } # a plugin in plugins/live/jama.py
Scopes decide what each caller may reach through the same `docs:` entries the ingest uses.
"""
import importlib.util, inspect, logging, os, pathlib, sys
from .base import LiveSource

log = logging.getLogger("gateway.live")
REGISTRY: dict[str, type[LiveSource]] = {}
_instances: dict[str, LiveSource] = {}


def _register_module(mod, origin: str) -> None:
    for _, obj in inspect.getmembers(mod, inspect.isclass):
        if issubclass(obj, LiveSource) and obj is not LiveSource and obj.__module__ == mod.__name__ and obj.name != "base":
            REGISTRY[obj.name] = obj
            log.info("live source '%s' from %s", obj.name, origin)


def discover() -> None:
    d = pathlib.Path(os.environ.get("GATEWAY_PLUGINS_DIR", "/plugins/live"))
    if d.is_dir():
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
        for f in sorted(d.glob("*.py")):
            if f.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(f"live_plugins.{f.stem}", f)
                mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
                _register_module(mod, f"plugin {f.name}")
            except Exception:
                log.exception("live plugin %s failed to load; skipped", f.name)


def load(name: str, declared: dict | None = None) -> LiveSource:
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
        raise KeyError(f"unknown live source '{name}'; available: {sorted(REGISTRY)}")
    inst = klass(spec)
    inst.name = name
    _instances[name] = inst
    return inst


__all__ = ["LiveSource", "REGISTRY", "discover", "load"]

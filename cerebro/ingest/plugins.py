"""Ingest-side plugin registry: the `source` part of every plugin in the plugins directory (cerebro.yaml
gateway.plugins_dir, or $CEREBRO_PLUGINS_DIR), plus installed packages exposing a `cerebro.sources` entry point.
Nothing is built in. A scope references a plugin by name under `docs:`; the top-level `sources:` map carries the
plugin's non-secret options, and `sources.<name>.type: module:Class` swaps the implementation behind a name.
"""
from __future__ import annotations
import importlib, importlib.metadata, logging, os
import cerebro.sdk  # noqa: F401  (registers the `stack_plugins` alias before any plugin file is imported)
from cerebro.core.config import Config
from cerebro.core.contracts.sources import Source, discover

log = logging.getLogger("cerebro.ingest.plugins")
ENTRY_POINT_GROUP = "cerebro.sources"


class SourceRegistry:
    def __init__(self, config: Config, plugins_dir: str | os.PathLike | None = None):
        self.config = config
        self.plugins_dir = str(plugins_dir or os.environ.get("CEREBRO_PLUGINS_DIR") or config.gateway.plugins_dir)
        self._classes: dict[str, type[Source]] | None = None
        self._origin: dict[str, str] = {}
        self._instances: dict[str, Source] = {}

    def discover(self) -> dict[str, type[Source]]:
        classes: dict[str, type[Source]] = {}
        for name, plugin in discover(self.plugins_dir).items():
            if plugin.source is not None:
                classes[name] = plugin.source
                self._origin[name] = f"plugin {plugin.source.__module__.rsplit('.', 1)[-1]}.py"
        try:
            eps = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
        except TypeError:                                              # very old importlib.metadata
            eps = importlib.metadata.entry_points().get(ENTRY_POINT_GROUP, [])
        for ep in eps:
            try:
                cls = ep.load()
            except Exception:
                log.exception("entry point %s failed to load; skipped", ep.name)
                continue
            if not getattr(cls, "name", None) or cls.name == "base":
                cls.name = ep.name
            classes[cls.name] = cls
            self._origin[cls.name] = f"entry point {ep.value}"
        self._classes = classes
        return classes

    @property
    def classes(self) -> dict[str, type[Source]]:
        if self._classes is None:
            self.discover()
        return self._classes

    def available(self) -> dict[str, str]:
        """plugin name -> where it came from."""
        self.classes
        return dict(self._origin)

    def load(self, name: str) -> Source:
        """Instantiate the source part of plugin `name` with its `sources:` options (cached per name)."""
        if name in self._instances:
            return self._instances[name]
        options = dict(self.config.sources.get(name) or {})
        type_ = str(options.pop("type", name))
        if ":" in type_:
            mod, cls = type_.split(":", 1)
            klass = getattr(importlib.import_module(mod), cls)
        elif type_ in self.classes:
            klass = self.classes[type_]
        else:
            raise KeyError(f"no plugin with an ingest part named '{name}'; available: {sorted(self.classes)}. "
                           f"Add {self.plugins_dir}/{name}.py or a package with a {ENTRY_POINT_GROUP} entry point.")
        inst = klass(options)
        inst.name = name
        self._instances[name] = inst
        return inst

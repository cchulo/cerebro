"""Adapter registry. Built-in adapters are looked up by name; custom ones by "package.module:ClassName"
declared under `sources:` in config/scopes.yaml, e.g.

sources:
  jira: { type: "my_adapters.jira:JiraSource", project_filter: "status != Closed" }

Any class deriving from sources.base.Source works; options from the entry are passed to its constructor.
"""
import importlib
from .base import Source, Document, ScopeContext
from .confluence import ConfluenceSource
from .backstage import BackstageSource
from .git import GitDocsSource
from .files import FilesSource

BUILTIN: dict[str, type[Source]] = {c.name: c for c in (ConfluenceSource, BackstageSource, GitDocsSource, FilesSource)}
_instances: dict[str, Source] = {}


def load(name: str, declared: dict | None) -> Source:
    """Instantiate adapter `name` from its `sources:` declaration (or the built-in of that name)."""
    if name in _instances:
        return _instances[name]
    spec = dict(declared or {})
    type_ = spec.pop("type", name)
    if ":" in type_:
        mod, cls = type_.split(":", 1)
        klass = getattr(importlib.import_module(mod), cls)
    elif type_ in BUILTIN:
        klass = BUILTIN[type_]
    else:
        raise KeyError(f"unknown source type '{type_}' for '{name}'; built-ins: {sorted(BUILTIN)}")
    inst = klass(spec)
    inst.name = name
    _instances[name] = inst
    return inst


__all__ = ["Source", "Document", "ScopeContext", "BUILTIN", "load"]

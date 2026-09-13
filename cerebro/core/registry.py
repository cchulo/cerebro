"""Find the adapter class for a `type:` in cerebro.yaml.

    type: lightrag              -> cerebro.adapters.<kind>.lightrag:Adapter   (in-tree, by convention)
    type: mypkg.engines:Fancy   -> that module and class                       (out-of-tree, any pip package)

No registry file to edit, so adapters never conflict over a shared module.
"""
from __future__ import annotations
import importlib
from typing import Any
from .context import Adapter, AdapterContext

KINDS: dict[str, str] = {
    "identity": "cerebro.adapters.identity",
    "auth": "cerebro.adapters.auth",            # AuthorizationServer (builtin mode)
    "policy": "cerebro.adapters.policy",
    "docs": "cerebro.adapters.docs",
    "code": "cerebro.adapters.code",
    "memory": "cerebro.adapters.memory",
    "inference": "cerebro.adapters.inference",
    "provision": "cerebro.adapters.provision",
    "state": "cerebro.adapters.state",
}


def resolve(kind: str, type_: str) -> type[Adapter]:
    if ":" in type_:
        mod_name, cls_name = type_.split(":", 1)
    else:
        if kind not in KINDS:
            raise KeyError(f"unknown adapter kind '{kind}'; known: {sorted(KINDS)}")
        mod_name, cls_name = f"{KINDS[kind]}.{type_}", "Adapter"
    try:
        mod = importlib.import_module(mod_name)
    except ModuleNotFoundError as e:
        if e.name and mod_name.startswith(e.name):
            raise LookupError(f"no {kind} adapter of type '{type_}' (module {mod_name} not found)") from e
        raise
    cls = getattr(mod, cls_name, None)
    if cls is None or not (isinstance(cls, type) and issubclass(cls, Adapter)):
        raise LookupError(f"{mod_name} has no Adapter subclass named '{cls_name}'")
    return cls


def build(kind: str, type_: str, options: dict[str, Any] | None, ctx: AdapterContext) -> Adapter:
    inst = resolve(kind, type_)(options or {}, ctx)
    if getattr(inst, "name", "base") in ("base", None):
        inst.name = type_
    return inst

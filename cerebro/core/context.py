"""What every adapter is constructed with, and the base class they share.

    Adapter(options, ctx)

`options` is the adapter's own `options:` map from cerebro.yaml. `ctx` gives it the whole config (scopes, inference
endpoints), a Secrets source (never the values in config), and a Locator that turns a unit name into a URL, so no
adapter ever hard-codes `http://lightrag-<scope>:9621` again.
"""
from __future__ import annotations
import os
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from .config import Config

if TYPE_CHECKING:
    from .contracts.provision import UnitSpec, JobSpec


@runtime_checkable
class Secrets(Protocol):
    def get(self, name: str, default: str | None = None) -> str | None: ...


class EnvSecrets:
    """Secrets from the process environment (compose `env_file`, Kubernetes `envFrom: secretRef`)."""
    def get(self, name: str, default: str | None = None) -> str | None:
        return os.environ.get(name, default)


class Locator(ABC):
    """unit name -> base URL. Provisioners implement it; tests use StaticLocator."""
    @abstractmethod
    def endpoint(self, unit: str) -> str: ...


class StaticLocator(Locator):
    def __init__(self, urls: dict[str, str] | None = None, template: str = "http://{unit}"):
        self.urls, self.template = dict(urls or {}), template

    def endpoint(self, unit: str) -> str:
        return self.urls.get(unit) or self.template.format(unit=unit)


class AdapterContext:
    def __init__(self, config: Config, secrets: Secrets | None = None, locator: Locator | None = None, inference: Any = None):
        self.config = config
        self.secrets: Secrets = secrets or EnvSecrets()
        self.locator: Locator = locator or StaticLocator()
        self.inference = inference          # an Inference adapter, for adapters that need a model themselves

    def secret(self, name: str | None, default: str | None = None) -> str | None:
        return self.secrets.get(name, default) if name else default


class Adapter(ABC):
    """Base of every contract. `kind` and `name` identify it in logs and manifests."""
    kind: str = "adapter"
    name: str = "base"

    def __init__(self, options: dict[str, Any] | None = None, ctx: AdapterContext | None = None):
        self.options: dict[str, Any] = dict(options or {})
        self.ctx = ctx

    def option(self, key: str, default=None):
        return self.options.get(key, default)

    def configured(self) -> bool:
        """False when credentials or endpoints are missing; callers skip the adapter with a note instead of failing."""
        return True

    # ---- what the provisioner should run for this adapter (engine instances, indexer jobs). Default: nothing.
    def units(self) -> "list[UnitSpec]":
        return []

    def jobs(self) -> "list[JobSpec]":
        return []

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.kind}:{self.name}>"

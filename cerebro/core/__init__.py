"""cerebro.core: everything the gateway and the ingest engine are allowed to depend on.

Nothing in this package imports an engine client, a web framework or a Kubernetes library. Adapters implement the
contracts in `cerebro.core.contracts`; `cerebro.core.registry` finds them by the `type:` written in cerebro.yaml.
"""
from .types import Health
from .principal import Principal, Grants, TokenScope
from .config import Config, load_config, json_schema
from .context import AdapterContext, Adapter, Secrets, EnvSecrets, Locator, StaticLocator
from .units import CodeUnit, code_units, docs_unit_name, slug

__all__ = ["Health", "Principal", "Grants", "TokenScope", "Config", "load_config", "json_schema",
           "AdapterContext", "Adapter", "Secrets", "EnvSecrets", "Locator", "StaticLocator",
           "CodeUnit", "code_units", "docs_unit_name", "slug"]

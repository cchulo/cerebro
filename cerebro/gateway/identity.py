"""identity.mode -> IdentityProvider.

    none      -> cerebro.adapters.identity.none
    static    -> cerebro.adapters.identity.static
    builtin   -> bearer_jwt | bearer_introspect (identity.token_validation)
    external  -> bearer_jwt | bearer_introspect

When `identity.legacy` is set, the result is a Chain: the bearer adapter first, trusted proxy headers second, so
an SSO proxy can keep working while clients move to tokens. `policy.options` never reach identity adapters; the
identity adapters read their block of the config through the AdapterContext.
"""
from __future__ import annotations
from cerebro.core import AdapterContext, Config, Principal, registry
from cerebro.core.contracts import IdentityProvider, RequestInfo
from cerebro.core.types import Unauthenticated


class Chain(IdentityProvider):
    """Try each provider in order; the first Principal wins. The 401 challenge and the protected-resource
    metadata come from the first provider (the bearer one), which is what MCP clients need to see."""
    name = "chain"

    def __init__(self, providers: list[IdentityProvider], options=None, ctx=None):
        super().__init__(options, ctx)
        if not providers:
            raise ValueError("identity chain needs at least one provider")
        self.providers = providers

    async def resolve(self, request: RequestInfo) -> Principal:
        errors: list[str] = []
        for p in self.providers:
            try:
                return await p.resolve(request)
            except Unauthenticated as e:
                errors.append(f"{p.name}: {e}")
        raise Unauthenticated("; ".join(errors))

    def challenge(self) -> dict[str, str]:
        return self.providers[0].challenge()

    def protected_resource_metadata(self) -> dict | None:
        for p in self.providers:
            md = p.protected_resource_metadata()
            if md is not None:
                return md
        return None


def identity_type(config: Config) -> str:
    ident = config.identity
    if ident.mode in ("none", "static"):
        return ident.mode
    return "bearer_introspect" if ident.token_validation == "introspection" else "bearer_jwt"


def build_identity(config: Config, ctx: AdapterContext) -> IdentityProvider:
    primary = registry.build("identity", identity_type(config), {}, ctx)
    if config.identity.legacy is None:
        return primary
    legacy = registry.build("identity", config.identity.legacy.type, {}, ctx)
    return Chain([primary, legacy], ctx=ctx)

"""trusted_headers: identity from headers an SSO reverse proxy sets (oauth2-proxy, Authelia, Caddy OIDC).

The v1 model, kept for organisations whose edge already does the OIDC flow. Configured by `identity.legacy`
(user_header, groups_header); the adapter's own options override those names. Groups are comma separated;
`policy.always_groups` is added by the policy, never here. Only safe when the proxy is the only thing that can
reach the gateway.
"""
from __future__ import annotations
from cerebro.core import Principal
from cerebro.core.config import LegacyIdentity
from cerebro.core.contracts import IdentityProvider, RequestInfo
from cerebro.core.types import Unauthenticated

ISSUER = "trusted-headers"


class Adapter(IdentityProvider):
    name = "trusted_headers"

    def __init__(self, options=None, ctx=None):
        super().__init__(options, ctx)
        legacy = (ctx.config.identity.legacy if ctx else None) or LegacyIdentity()
        self.user_header = str(self.option("user_header", legacy.user_header)).lower()
        self.groups_header = str(self.option("groups_header", legacy.groups_header)).lower()

    async def resolve(self, request: RequestInfo) -> Principal:
        user = (request.headers.get(self.user_header) or "").strip()
        if not user:
            raise Unauthenticated(f"missing {self.user_header} header; is the gateway behind the SSO proxy?")
        groups = frozenset(g.strip() for g in request.headers.get(self.groups_header, "").split(",") if g.strip())
        return Principal(subject=user, groups=groups, issuer=ISSUER)

    def challenge(self) -> dict[str, str]:
        return {"WWW-Authenticate": f'Bearer error="invalid_request", error_description="{self.user_header} header required"'}

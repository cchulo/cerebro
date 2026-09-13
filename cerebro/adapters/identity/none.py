"""identity.mode none: one person on their own machine. Every request is the configured `principal:`.

The gateway binds to loopback in this mode, and this adapter refuses anything that is not a loopback request
unless `identity.allow_remote` is true. With allow_remote, every request (loopback included, so a same-host proxy
cannot bypass it) must carry `Authorization: Bearer <value of the secret named identity.static_token_env>`
(default CEREBRO_TOKEN), so a LAN exposure is never open.
"""
from __future__ import annotations
import hmac
from cerebro.core import Principal
from cerebro.core.contracts import IdentityProvider, RequestInfo
from cerebro.core.types import Unauthenticated
from ._claims import principal_from_seed


class Adapter(IdentityProvider):
    name = "none"

    async def resolve(self, request: RequestInfo) -> Principal:
        ident = self.ctx.config.identity
        if ident.allow_remote:
            expected = self.ctx.secret(ident.static_token_env)
            if not expected:
                raise Unauthenticated(f"identity.allow_remote is set but secret {ident.static_token_env} is empty")
            if not hmac.compare_digest(request.bearer() or "", expected):
                raise Unauthenticated("missing or invalid bearer token")
        elif not request.is_loopback:
            raise Unauthenticated("identity.mode none accepts loopback requests only (set identity.allow_remote to change that)")
        return principal_from_seed(ident.principal, issuer=None)

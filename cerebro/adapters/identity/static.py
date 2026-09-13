"""identity.mode static: `identity.tokens` maps bearer tokens to principals. Tests and demos; never production."""
from __future__ import annotations
import hmac
from cerebro.core import Principal
from cerebro.core.contracts import IdentityProvider, RequestInfo
from cerebro.core.types import Unauthenticated
from ._claims import principal_from_seed

ISSUER = "static"


class Adapter(IdentityProvider):
    name = "static"

    async def resolve(self, request: RequestInfo) -> Principal:
        token = request.bearer()
        if not token:
            raise Unauthenticated("missing bearer token")
        for candidate, seed in self.ctx.config.identity.tokens.items():
            if hmac.compare_digest(candidate, token):
                return principal_from_seed(seed, issuer=ISSUER)
        raise Unauthenticated("unknown bearer token")

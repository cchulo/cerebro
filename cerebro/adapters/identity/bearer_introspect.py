"""bearer_introspect: RFC 7662 token introspection, for opaque access tokens (or when revocation must be immediate).

identity.introspection names the endpoint (default: `introspection_endpoint` from the issuer's discovery document),
the client id and the secret name holding the client secret; the gateway authenticates to the endpoint with HTTP
basic auth. An inactive token, or one for another issuer or audience, is Unauthenticated. The response's claims
map to a Principal exactly as JWT claims do (cerebro.adapters.identity._claims.principal_from_claims).
"""
from __future__ import annotations
from typing import Any
import httpx
from cerebro.core import Principal
from cerebro.core.contracts import RequestInfo
from cerebro.core.types import Unauthenticated
from ._bearer import BearerBase
from ._claims import principal_from_claims


class Adapter(BearerBase):
    name = "bearer_introspect"

    def configured(self) -> bool:
        return super().configured() and self.identity.introspection is not None

    async def endpoint(self) -> str:
        intro = self.identity.introspection
        if intro is None:
            raise RuntimeError("bearer_introspect needs an identity.introspection block")
        if intro.url:
            return intro.url
        url = (await self.issuer_metadata()).get("introspection_endpoint")
        if not url:
            raise Unauthenticated(f"issuer {self.issuer} publishes no introspection_endpoint")
        return str(url)

    async def introspect(self, token: str) -> dict[str, Any]:
        intro = self.identity.introspection
        secret = self.ctx.secret(intro.client_secret_env) or ""
        url = await self.endpoint()
        try:
            async with self._client() as h:
                r = await h.post(url, data={"token": token, "token_type_hint": "access_token"},
                                 auth=(intro.client_id, secret), headers={"Accept": "application/json"})
                r.raise_for_status()
                return r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise Unauthenticated(f"introspection at {url} failed: {e}") from e

    async def resolve(self, request: RequestInfo) -> Principal:
        token = request.bearer()
        if not token:
            raise Unauthenticated("missing bearer token")
        claims = await self.introspect(token)
        if not claims.get("active"):
            raise Unauthenticated("token is not active")
        self.check_issuer_and_audience(claims)
        return principal_from_claims(claims, groups_claim=self.identity.groups_claim,
                                     scope_claim=self.identity.scope_claim, issuer=self.issuer)

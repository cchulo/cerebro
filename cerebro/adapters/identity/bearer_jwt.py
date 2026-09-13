"""bearer_jwt: OAuth 2.1 / OIDC access tokens as JWTs, validated locally against the issuer's JWKS.

Used by identity.mode builtin (Keycloak the stack runs) and external (the organisation's IdP): same code, a
different issuer URL. Checks, in PyJWT: signature against a key from the JWKS (identity.jwks_url, or `jwks_uri`
from the issuer's discovery document), `iss` == identity.issuer, `aud` contains the RFC 8707 resource identifier
(Config.resource_id(), or identity.audience), `exp`. The JWKS is cached (options.jwks_ttl, default 1h) and
refetched once when a token names an unknown `kid` (key rotation).

Claims -> Principal: see cerebro.adapters.identity._claims.principal_from_claims (groups_claim, scope_claim,
service detection). Options: algorithms (default: the asymmetric ones), leeway (seconds), jwks_ttl, timeout.
"""
from __future__ import annotations
import time
from typing import Any
import httpx
import jwt
from cerebro.core import Principal
from cerebro.core.contracts import RequestInfo
from cerebro.core.types import Unauthenticated
from ._bearer import BearerBase
from ._claims import principal_from_claims

ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]


class Adapter(BearerBase):
    name = "bearer_jwt"

    def __init__(self, options=None, ctx=None):
        super().__init__(options, ctx)
        self.algorithms: list[str] = list(self.option("algorithms", ALGORITHMS))
        self.leeway = float(self.option("leeway", 30))
        self.jwks_ttl = float(self.option("jwks_ttl", 3600))
        self._jwks: jwt.PyJWKSet | None = None
        self._jwks_at = 0.0

    # ---- keys
    async def jwks_url(self) -> str:
        url = self.option("jwks_url") or self.identity.jwks_url
        if url:
            return str(url)
        url = (await self.issuer_metadata()).get("jwks_uri")
        if not url:
            raise Unauthenticated(f"issuer {self.issuer} publishes no jwks_uri")
        return str(url)

    async def jwks(self, *, refresh: bool = False) -> jwt.PyJWKSet:
        if self._jwks is not None and not refresh and time.monotonic() - self._jwks_at < self.jwks_ttl:
            return self._jwks
        url = await self.jwks_url()
        try:
            async with self._client() as h:
                r = await h.get(url, headers={"Accept": "application/json"})
                r.raise_for_status()
                doc = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise Unauthenticated(f"cannot fetch JWKS from {url}: {e}") from e
        try:
            self._jwks = jwt.PyJWKSet.from_dict(doc)
        except jwt.PyJWTError as e:
            raise Unauthenticated(f"JWKS at {url} is unusable: {e}") from e
        self._jwks_at = time.monotonic()
        return self._jwks

    async def signing_key(self, token: str) -> jwt.PyJWK:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise Unauthenticated(f"malformed token: {e}") from e
        kid = header.get("kid")
        for attempt in (False, True):                      # second pass refetches: the issuer may have rotated keys
            keyset = await self.jwks(refresh=attempt)
            if kid is None:
                if len(keyset.keys) == 1:
                    return keyset.keys[0]
            else:
                try:
                    return keyset[kid]
                except KeyError:
                    pass
        raise Unauthenticated("token signed with a key the issuer does not publish" if kid else "token has no kid and the issuer publishes several keys")

    # ---- resolve
    async def resolve(self, request: RequestInfo) -> Principal:
        token = request.bearer()
        if not token:
            raise Unauthenticated("missing bearer token")
        key = await self.signing_key(token)
        algorithms = [key.algorithm_name] if key.algorithm_name and key.algorithm_name in self.algorithms else self.algorithms
        try:
            claims: dict[str, Any] = jwt.decode(token, key.key, algorithms=algorithms, audience=self.audiences(),
                                                issuer=self.issuer, leeway=self.leeway,
                                                options={"require": ["exp", "iss", "aud", "sub"]})
        except jwt.PyJWTError as e:
            raise Unauthenticated(f"invalid token: {e}") from e
        return principal_from_claims(claims, groups_claim=self.identity.groups_claim,
                                     scope_claim=self.identity.scope_claim, issuer=self.issuer)

"""Shared base of the bearer-token adapters: issuer discovery (RFC 8414 / OIDC), the RFC 9728 protected-resource
document and the 401 challenge. Not an adapter itself.

The resource identifier (RFC 8707) is `Config.resource_id()`: gateway.public_url or host:port + path. The
protected-resource metadata is served by the gateway at `<origin>/.well-known/oauth-protected-resource` (and the
RFC 9728 path-suffixed form), which is what the challenge points at.
"""
from __future__ import annotations
import asyncio
import time
from typing import Any
from urllib.parse import urlsplit
import httpx
from cerebro.core import TokenScope
from cerebro.core.config import IdentityConfig
from cerebro.core.contracts import IdentityProvider
from cerebro.core.types import Unauthenticated

WELL_KNOWN = "/.well-known/oauth-protected-resource"


def resource_origin(resource: str) -> str:
    u = urlsplit(resource)
    return f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else resource.rstrip("/")


def metadata_url(resource: str) -> str:
    return resource_origin(resource) + WELL_KNOWN


def discovery_urls(issuer: str) -> list[str]:
    """OIDC discovery first, then RFC 8414 in both the appended and the path-inserted form."""
    issuer = issuer.rstrip("/")
    u = urlsplit(issuer)
    urls = [f"{issuer}/.well-known/openid-configuration", f"{issuer}/.well-known/oauth-authorization-server"]
    if u.path and u.path != "/":
        urls.append(f"{u.scheme}://{u.netloc}/.well-known/oauth-authorization-server{u.path}")
    return urls


class BearerBase(IdentityProvider):
    """Common plumbing: config access, the issuer's metadata (cached), challenge and RFC 9728 document."""

    def __init__(self, options=None, ctx=None):
        super().__init__(options, ctx)
        self.timeout = float(self.option("timeout", 10))
        self.metadata_ttl = float(self.option("metadata_ttl", 3600))
        self._metadata: dict[str, Any] | None = None
        self._metadata_at = 0.0
        self._lock = asyncio.Lock()

    # ---- config
    @property
    def identity(self) -> IdentityConfig:
        return self.ctx.config.identity

    @property
    def issuer(self) -> str:
        iss = self.option("issuer") or self.identity.issuer
        if not iss:
            raise RuntimeError(f"identity adapter {self.name} needs identity.issuer")
        return str(iss).rstrip("/")

    @property
    def resource(self) -> str:
        return self.ctx.config.resource_id()

    def audiences(self) -> list[str]:
        """Accepted `aud` values: the RFC 8707 resource identifier, plus identity.audience when it differs."""
        out = [self.resource]
        if self.identity.audience and self.identity.audience not in out:
            out.append(self.identity.audience)
        return out

    def configured(self) -> bool:
        return bool(self.option("issuer") or self.identity.issuer)

    # ---- issuer metadata
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout)

    async def issuer_metadata(self, *, refresh: bool = False) -> dict[str, Any]:
        async with self._lock:
            fresh = self._metadata is not None and time.monotonic() - self._metadata_at < self.metadata_ttl
            if fresh and not refresh:
                return self._metadata
            last: Exception | None = None
            async with self._client() as h:
                for url in discovery_urls(self.issuer):
                    try:
                        r = await h.get(url, headers={"Accept": "application/json"})
                    except httpx.HTTPError as e:
                        last = e
                        continue
                    if r.status_code == 200:
                        doc = r.json()
                        if doc.get("issuer") and doc["issuer"].rstrip("/") != self.issuer:
                            raise Unauthenticated(f"issuer metadata at {url} names a different issuer {doc['issuer']}")
                        self._metadata, self._metadata_at = doc, time.monotonic()
                        return doc
            raise Unauthenticated(f"cannot discover authorization server metadata for {self.issuer}"
                                  + (f": {last}" if last else ""))

    # ---- RFC 9728
    def challenge(self) -> dict[str, str]:
        return {"WWW-Authenticate": f'Bearer resource_metadata="{metadata_url(self.resource)}"'}

    def protected_resource_metadata(self) -> dict:
        return {"resource": self.resource, "authorization_servers": [self.issuer],
                "scopes_supported": [s.value for s in TokenScope], "bearer_methods_supported": ["header"]}

    def check_issuer_and_audience(self, claims: dict[str, Any]) -> None:
        """For claim sets PyJWT did not validate (introspection responses): iss and aud when present."""
        iss = claims.get("iss")
        if iss and str(iss).rstrip("/") != self.issuer:
            raise Unauthenticated("token issued by a different issuer")
        aud = claims.get("aud")
        if aud is not None:
            values = {aud} if isinstance(aud, str) else set(aud)
            if not values & set(self.audiences()):
                raise Unauthenticated("token was not issued for this resource (aud)")

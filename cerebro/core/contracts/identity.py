"""Identity: request -> Principal. The gateway is always the OAuth 2.1 resource server; adapters decide how a
request proves who it is (bearer JWT, introspection, trusted proxy headers, a static map, or nothing at all).

The authorization server (who mints tokens) is a separate contract, `AuthorizationServer`, used only in
identity.mode `builtin`: it contributes the unit the provisioner runs and seeds users/groups.
"""
from __future__ import annotations
from abc import abstractmethod
from pydantic import BaseModel, Field
from ..context import Adapter
from ..principal import Principal
from ..config import UserSeed


class RequestInfo(BaseModel):
    """The slice of an HTTP request identity needs. Header names are lower-cased."""
    headers: dict[str, str] = Field(default_factory=dict)
    method: str = "POST"
    path: str = "/mcp"
    client_host: str | None = None

    @classmethod
    def from_headers(cls, headers, **kw) -> "RequestInfo":
        return cls(headers={str(k).lower(): str(v) for k, v in dict(headers).items()}, **kw)

    def bearer(self) -> str | None:
        auth = self.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        return token.strip() if scheme.lower() == "bearer" and token.strip() else None

    @property
    def is_loopback(self) -> bool:
        return self.client_host in (None, "127.0.0.1", "::1", "localhost")


class IdentityProvider(Adapter):
    kind = "identity"

    @abstractmethod
    async def resolve(self, request: RequestInfo) -> Principal:
        """Return the Principal or raise cerebro.core.types.Unauthenticated."""

    def challenge(self) -> dict[str, str]:
        """Headers for the 401 response. Bearer adapters point at the RFC 9728 metadata."""
        return {"WWW-Authenticate": "Bearer"}

    def protected_resource_metadata(self) -> dict | None:
        """RFC 9728 document served at /.well-known/oauth-protected-resource, or None when not applicable."""
        return None


class AuthorizationServer(Adapter):
    """identity.mode builtin: an off-the-shelf OAuth 2.1 / OIDC server the stack provisions (Keycloak first)."""
    kind = "auth"

    @abstractmethod
    def issuer(self) -> str: ...

    @abstractmethod
    async def seed(self, users: list[UserSeed], groups: list[str], resource_id: str) -> dict:
        """Idempotently create realm, groups, users, and register the gateway as a resource (audience)."""

    async def ready(self) -> bool:
        return True

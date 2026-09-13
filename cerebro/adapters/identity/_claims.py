"""Shared mapping helpers: config seeds and token claims -> Principal. Not an adapter."""
from __future__ import annotations
from typing import Any
from cerebro.core import Principal, TokenScope
from cerebro.core.config import PrincipalSeed
from cerebro.core.types import Unauthenticated


def principal_from_seed(seed: PrincipalSeed, issuer: str | None) -> Principal:
    """A `principal:` / `tokens:` entry from cerebro.yaml. token_scopes None means every scope."""
    scopes = frozenset(seed.token_scopes) if seed.token_scopes is not None else TokenScope.all()
    return Principal(subject=seed.subject, groups=frozenset(seed.groups), kind=seed.kind, token_scopes=scopes, issuer=issuer)


def as_list(value: Any) -> list[str]:
    """Claims arrive as JSON lists or as space/comma separated strings (Keycloak, Entra, RFC 7662 `scope`)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [v for v in value.replace(",", " ").split() if v]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(v) for v in value if v not in (None, "")]
    return [str(value)]


def claim(claims: dict[str, Any], path: str) -> Any:
    """`groups` or a dotted path such as `realm_access.roles`."""
    cur: Any = claims
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def principal_from_claims(claims: dict[str, Any], *, groups_claim: str = "groups", scope_claim: str = "scope",
                          issuer: str | None = None) -> Principal:
    """Validated token claims (JWT payload or an introspection response) -> Principal.

    - groups: the configured claim, list or delimited string
    - token scopes: only the cerebro:* values matter; a token that carries none of them (a plain OIDC IdP that
      knows nothing about cerebro) gets every scope, and `issuer` records who vouched for it
    - kind: `service` for client-credentials tokens, recognised by the absence of a human identity claim
      (email / preferred_username) together with client_id or azp equal to `sub`
    """
    sub = claims.get("sub") or claims.get("client_id") or claims.get("username")
    if not sub:
        raise Unauthenticated("token carries no subject")
    sub = str(sub)
    groups = frozenset(as_list(claim(claims, groups_claim)))
    granted = {s for s in as_list(claim(claims, scope_claim)) if s in TokenScope.all()}
    token_scopes = frozenset(granted) if granted else TokenScope.all()
    human = claims.get("email") or claims.get("preferred_username")
    client = claims.get("client_id") or claims.get("azp")
    kind = "service" if not human and client and str(client) == sub else "user"
    display = claims.get("name") or claims.get("preferred_username") or claims.get("email")
    return Principal(subject=sub, groups=groups, token_scopes=token_scopes, kind=kind,
                     display_name=str(display) if display else None, issuer=issuer or claims.get("iss"))

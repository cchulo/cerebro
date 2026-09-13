"""Who is calling, and what they may touch.

A `Principal` is what an IdentityProvider produces from a request. `Grants` is what the AccessPolicy derives from a
Principal and the configured scopes: the only thing tools consult. Tools never look at headers or tokens.
"""
from __future__ import annotations
import enum
from typing import Literal
from pydantic import BaseModel, Field
from .types import Forbidden


class TokenScope(str, enum.Enum):
    """Per-capability OAuth scopes. Every gateway tool declares the one it needs; the gateway hides tools whose scope
    the token lacks. Mode `none` grants all of them."""
    DOCS_READ = "cerebro:docs.read"
    CODE_READ = "cerebro:code.read"
    MEMORY_READ = "cerebro:memory.read"
    MEMORY_WRITE = "cerebro:memory.write"
    ADMIN = "cerebro:admin"

    @classmethod
    def all(cls) -> frozenset[str]:
        return frozenset(s.value for s in cls)


class Principal(BaseModel):
    subject: str                                   # stable user or service id (OIDC `sub`, or the configured local name)
    groups: frozenset[str] = frozenset()
    token_scopes: frozenset[str] = Field(default_factory=TokenScope.all)
    kind: Literal["user", "service"] = "user"
    display_name: str | None = None
    issuer: str | None = None                      # who vouched for this principal (None for mode `none`)

    def has_scope(self, scope: TokenScope | str) -> bool:
        value = scope.value if isinstance(scope, TokenScope) else scope
        return value in self.token_scopes or TokenScope.ADMIN.value in self.token_scopes

    @property
    def bank_slug(self) -> str:
        return "".join(c if c.isalnum() else "-" for c in self.subject.lower())


class RepoGrant(BaseModel):
    url: str
    scope: str
    branches: list[str] = Field(default_factory=list)   # [] = default branch only

    @property
    def name(self) -> str:
        return repo_name(self.url) or self.url


class Grants(BaseModel):
    """Computed by AccessPolicy, consulted by every tool. Ordered lists keep output deterministic."""
    subject: str
    scopes: list[str] = Field(default_factory=list)
    repos: list[RepoGrant] = Field(default_factory=list)
    banks: list[str] = Field(default_factory=list)          # every bank the caller may read/write
    personal_bank: str | None = None                          # None for service principals
    team_banks: list[str] = Field(default_factory=list)
    token_scopes: frozenset[str] = Field(default_factory=TokenScope.all)

    def check_scope(self, scope: str) -> None:
        if scope not in self.scopes:
            raise Forbidden(f"{self.subject} is not allowed to access scope '{scope}'")

    def check_scopes(self, scopes: list[str] | None) -> list[str]:
        targets = scopes or self.scopes
        for s in targets:
            self.check_scope(s)
        return targets

    def check_bank(self, bank: str | None) -> str:
        b = bank or self.personal_bank
        if b is None:
            raise Forbidden(f"{self.subject} has no personal bank; name a team bank from {self.banks}")
        if b not in self.banks:
            raise Forbidden(f"bank '{b}' not allowed; use one of {self.banks}")
        return b

    def check_token_scope(self, scope: TokenScope) -> None:
        if scope.value not in self.token_scopes and TokenScope.ADMIN.value not in self.token_scopes:
            raise Forbidden(f"token lacks scope {scope.value}")

    def repos_in(self, scopes: list[str] | None = None) -> list[RepoGrant]:
        wanted = set(scopes or self.scopes)
        return [r for r in self.repos if r.scope in wanted]


def repo_name(url: str) -> str | None:
    """https://github.com/org/x.git | git@github.com:org/x.git -> github.com/org/x"""
    import re
    m = re.match(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^/:]+)[:/](.+?)(?:\.git)?/?$", url.strip())
    return f"{m.group(1)}/{m.group(2)}".lower() if m else None

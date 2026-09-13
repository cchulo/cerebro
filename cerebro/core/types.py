"""Small shared types."""
from __future__ import annotations
from pydantic import BaseModel, Field


class Health(BaseModel):
    """Result of any adapter's health check. `data` carries engine-specific detail (documents processed, versions)."""
    ok: bool
    detail: str | None = None
    data: dict = Field(default_factory=dict)

    @classmethod
    def up(cls, detail: str | None = None, **data) -> "Health":
        return cls(ok=True, detail=detail, data=data)

    @classmethod
    def down(cls, detail: str, **data) -> "Health":
        return cls(ok=False, detail=detail, data=data)


class CerebroError(Exception):
    """Base class for errors the gateway turns into tool errors."""


class Unauthenticated(CerebroError):
    """No usable identity on the request. The gateway answers 401 with the identity provider's challenge."""


class Forbidden(CerebroError):
    """An authenticated principal asked for a scope, bank, unit or tool scope it does not hold."""


class Unsupported(CerebroError):
    """The adapter or unit does not offer this capability (e.g. `branch` on an engine without branches)."""

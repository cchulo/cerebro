"""Units: what gets its own workload.

A scope is the access boundary. Underneath it, `engines.code.unit` (overridable per scope) decides whether the code
engine runs one workload per scope (repos side by side) or one per repository. Docs engines are always one per scope.
Unit names double as service hostnames, so they are DNS labels.
"""
from __future__ import annotations
import hashlib, re
from typing import Literal
from pydantic import BaseModel, Field
from .config import Config, RepoSpec
from .principal import RepoGrant

_UNSAFE = re.compile(r"[^a-z0-9-]+")


def slug(value: str, max_len: int = 40) -> str:
    """DNS-label-safe slug; long or lossy inputs get a short hash suffix so distinct inputs stay distinct."""
    base = _UNSAFE.sub("-", value.lower()).strip("-")
    if len(base) <= max_len and base and _UNSAFE.sub("", value.lower()) == base.replace("-", "") and "-" not in value:
        return base
    h = hashlib.sha1(value.encode()).hexdigest()[:6]
    return f"{base[: max_len - 7].rstrip('-')}-{h}" if base else h


def repo_slug(repo: RepoSpec | RepoGrant) -> str:
    return slug(repo.name.rsplit("/", 1)[-1] + "-" + repo.name, 30)


class CodeUnit(BaseModel):
    name: str                                   # e.g. code-payments, code-payments-jinja-a1b2c3
    scope: str
    kind: Literal["scope", "repo"]
    repos: list[RepoSpec] = Field(default_factory=list)

    def branches_for(self, repo_url: str) -> list[str]:
        for r in self.repos:
            if r.key == repo_url.rstrip("/").removesuffix(".git").lower():
                return r.branches
        return []


def docs_unit_name(scope: str) -> str:
    return f"docs-{scope}"


def code_units(config: Config, scopes: list[str] | None = None) -> list[CodeUnit]:
    out: list[CodeUnit] = []
    for name, sc in config.scopes.items():
        if scopes is not None and name not in scopes:
            continue
        if not sc.code.repos:
            continue
        if config.scope_unit(name) == "scope":
            out.append(CodeUnit(name=f"code-{name}", scope=name, kind="scope", repos=list(sc.code.repos)))
        else:
            for r in sc.code.repos:
                out.append(CodeUnit(name=f"code-{name}-{repo_slug(r)}", scope=name, kind="repo", repos=[r]))
    return out


def units_for_repos(config: Config, repos: list[RepoGrant]) -> list[tuple[CodeUnit, list[RepoGrant]]]:
    """Group a caller's repo grants by the unit that serves each; the gateway fans out per unit."""
    keys = {r.url.rstrip("/").removesuffix(".git").lower(): r for r in repos}
    out: list[tuple[CodeUnit, list[RepoGrant]]] = []
    for unit in code_units(config, sorted({r.scope for r in repos})):
        mine = [keys[r.key] for r in unit.repos if r.key in keys]
        if mine:
            out.append((unit, mine))
    return out

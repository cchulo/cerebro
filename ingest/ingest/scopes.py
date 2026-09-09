"""Scope configuration for ingestion (config/scopes.yaml)."""
import os, yaml

CFG = yaml.safe_load(open(os.environ.get("SCOPES_FILE", "/config/scopes.yaml")))
SCOPES: dict[str, dict] = CFG["scopes"]
SOURCES: dict[str, dict] = CFG.get("sources") or {}       # optional adapter declarations


def lightrag_url(scope: str) -> str:
    return os.environ.get("LIGHTRAG_URL_TEMPLATE", "http://lightrag-{scope}:9621").format(scope=scope)


def docs_config(scope: str) -> dict[str, dict]:
    """adapter name -> its config for this scope. `docs: {backstage: ~}` means {} ."""
    return {name: (cfg or {}) for name, cfg in (SCOPES[scope].get("docs") or {}).items()}


def source_names() -> list[str]:
    names = []
    for s in SCOPES:
        for n in docs_config(s):
            if n not in names:
                names.append(n)
    return names


def validate() -> None:
    """A Confluence space or repo must belong to exactly one scope (they are the isolation unit)."""
    seen: dict[tuple, str] = {}
    for scope, sc in SCOPES.items():
        for r in sc.get("repos", []):
            k = ("repo", r.rstrip("/").removesuffix(".git").lower())
            if k in seen and seen[k] != scope:
                raise ValueError(f"repo {r} is listed in scopes {seen[k]} and {scope}")
            seen[k] = scope
        for sp in docs_config(scope).get("confluence", {}).get("spaces", []):
            k = ("space", sp)
            if k in seen and seen[k] != scope:
                raise ValueError(f"Confluence space {sp} is listed in scopes {seen[k]} and {scope}")
            seen[k] = scope


validate()

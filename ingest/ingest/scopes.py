"""Scope routing for ingestion: which LightRAG instance a document belongs to."""
import os, yaml

CFG = yaml.safe_load(open(os.environ.get("SCOPES_FILE", "/config/scopes.yaml")))
SCOPES: dict[str, dict] = CFG["scopes"]
RESTRICTED_PAGES = CFG.get("restricted_pages", "skip")

def lightrag_url(scope: str) -> str:
    return os.environ.get("LIGHTRAG_URL_TEMPLATE", "http://lightrag-{scope}:9621").format(scope=scope)

def scope_for_space(space: str) -> str | None:
    hits = [n for n, s in SCOPES.items() if space in s.get("confluence_spaces", [])]
    if len(hits) > 1:
        raise ValueError(f"Confluence space {space} is listed in more than one scope: {hits}")
    return hits[0] if hits else None

def scope_for_repo(url: str) -> str | None:
    norm = url.rstrip("/").removesuffix(".git").lower()
    hits = [n for n, s in SCOPES.items() if norm in [r.rstrip("/").removesuffix(".git").lower() for r in s.get("repos", [])]]
    if len(hits) > 1:
        raise ValueError(f"repo {url} is listed in more than one scope: {hits}")
    return hits[0] if hits else None

def backstage_scopes() -> list[str]:
    return [n for n, s in SCOPES.items() if s.get("backstage")]

def all_spaces() -> list[tuple[str, str]]:
    return [(sp, n) for n, s in SCOPES.items() for sp in s.get("confluence_spaces", [])]

def all_repos() -> list[tuple[str, str]]:
    return [(r, n) for n, s in SCOPES.items() for r in s.get("repos", [])]

"""Adapter contract for document sources.

An adapter turns *something* (a wiki, a requirements tool, a catalog, a git repo, a directory, an HTTP API) into a
stream of Documents for one scope. The sync engine (ingest/sync.py) does the rest: version diffing, batching into the
scope's LightRAG instance, deletion reconciling and state. Adapters therefore only need to *list* their documents;
they never talk to LightRAG or to the state store, and they never need to be registered anywhere: any subclass of
Source found in the plugins directory, in an installed package's entry points, or among the built-ins is available
under its `name`.

Rules an adapter must follow:
- `key` is stable across runs and unique within (adapter, scope). It becomes part of the LightRAG source id.
- `version` changes whenever `text` changes (a version number, an updated timestamp, a content hash).
- Yield only what the scope is allowed to contain. Anything with a finer-grained ACL than the scope
  (e.g. a page with its own read restriction) must be skipped, not yielded.
- Never yield source code; code goes to Sourcebot/CodeGraphContext, not LightRAG.
- Read credentials from the environment (`self.env`), non-secret settings from `sources:` options
  (`self.option`) or the scope's `docs:` entry (`ctx.config`).
"""
import os
from dataclasses import dataclass
from typing import Iterator


@dataclass
class Document:
    key: str            # stable id within (adapter, scope), e.g. "ENG/12345", "docs/adr/0001.md"
    version: str        # changes iff text changes
    text: str           # markdown/plain text handed to LightRAG
    title: str = ""     # optional heading prepended to the text


@dataclass
class ScopeContext:
    """What the engine knows about the scope being synced."""
    scope: str
    config: dict        # this adapter's entry under the scope's `docs:` map, e.g. {"spaces": ["ENG"]}
    repos: list[str]    # the scope's code repositories (for adapters that read docs out of repos)


class Source:
    """Base class for adapters. Subclass, set `name`, implement `documents()`."""
    name: str = "base"

    def __init__(self, options: dict | None = None):
        self.options = options or {}      # the adapter's `sources:` entry in scopes.yaml (minus `type`)

    # ---- settings helpers -------------------------------------------------------------------------------------
    def env(self, key: str, default: str = "") -> str:
        """Environment variable (secrets, URLs). Prefer these over options for anything sensitive."""
        return os.environ.get(key, default)

    def option(self, key: str, default=None):
        """Non-secret setting from the `sources:` declaration."""
        return self.options.get(key, default)

    # ---- contract ---------------------------------------------------------------------------------------------
    def configured(self) -> bool:
        """False when credentials/endpoints are missing; the engine then skips the adapter with a note."""
        return True

    def documents(self, ctx: ScopeContext, filter: dict | None = None) -> Iterator[Document]:
        """Yield every document that should exist in this scope (optionally narrowed by a webhook filter)."""
        raise NotImplementedError

    def covers(self, key: str, filter: dict | None) -> bool:
        """Whether a *previously seen* key is inside the set `documents(filter)` enumerates.
        The engine deletes keys that are covered but were not yielded. Without a filter everything is covered."""
        return filter is None or not filter

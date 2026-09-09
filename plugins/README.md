# plugins/ — every source is a plugin

Nothing that reads a source is built into the ingest or the gateway. Confluence, Backstage, git docs and local
files ship here as ordinary plugin files, and org-specific sources (JAMA, SharePoint, an internal API) are added the
same way: one file, auto-discovered, no rebuild.

| Directory | Loaded by | Base class | Referenced from `config/scopes.yaml` |
|---|---|---|---|
| `plugins/sources/` | ingest (`/plugins/sources`) | `ingest.sources.Source` | a scope's `docs:` entry, by `name` |
| `plugins/live/` | gateway (`/plugins/live`) | `gateway.live.LiveSource` | the top-level `live:` map, by `name` |

Shipped: `sources/confluence.py`, `sources/backstage.py`, `sources/git.py`, `sources/files.py`, `sources/jama.py`
(unverified example), `live/confluence.py` (REST or upstream MCP). Secrets come from `config/stack.env`; non-secret
options from `sources:` / `live:`. Develop an ingest adapter without LightRAG: `make source-check SCOPE=... SOURCE=...`.
Contract and walkthrough: `docs/SOURCES.md`.

# plugins/ — every source is one plugin file

Nothing that reads a source is built into the ingest or the gateway. Each `*.py` here is imported at startup by the
ingest (its `source` part), the gateway (its `live` part) and `make gen` (its `mcp` upstream image), and referenced
from `config/scopes.yaml` by name. Shipped: `confluence.py` (ingest + live fallback + mcp-atlassian upstream),
`backstage.py`, `git.py`, `files.py`, `jama.py` (example, unverified). How to write one: `docs/PLUGINS.md`.

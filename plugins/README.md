# plugins/: every source is one plugin file

Nothing that reads a source is built into the ingest or the gateway. Each `*.py` here is imported at startup by the
ingest (its `source` part), the gateway (its `live` part) and the provisioner's plan (its `mcp` upstream image, run
as unit `mcp-<name>`), and referenced from `cerebro.yaml` by name: a scope's `docs:` for what to ingest, the
top-level `sources:` for options and live overrides. Shipped: `confluence.py` (ingest + live fallback + mcp-atlassian
upstream), `backstage.py`, `git.py`, `files.py`, `jama.py` (example, unverified against a live instance). How to
write one: `docs/PLUGINS.md`; try one with `cerebro ingest -c cerebro.yaml check <scope> <source>`.

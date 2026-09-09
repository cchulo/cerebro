# plugins/ — drop-in document source adapters

Every `*.py` file here is loaded by the ingest service at startup (mounted at `/plugins`); each `Source` subclass in
it becomes available under its `name`, with no registration and no image rebuild. Reference it from a scope in
`config/scopes.yaml`:

```yaml
scopes:
  payments:
    docs:
      jama: { projects: [42] }        # <- adapter name, adapter-specific config
```

Secrets come from the environment (`config/stack.env`), non-secret options from an optional top-level `sources:`
entry. `jama.py` is a complete example. Develop against a real system without touching LightRAG:

```sh
make source-check SCOPE=payments SOURCE=jama
```

Contract and rules: `ingest/ingest/sources/base.py`; walkthrough: `docs/SOURCES.md`.

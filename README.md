# cerebro

Self-hosted context for AI agents: one MCP endpoint in front of your documents, your code and the agents' own memory,
with access control that lives in the index, not in a filter. Every engine behind the gateway is a plugin.

**Status: v2 is being built on `main` from [docs/DESIGN-V2.md](docs/DESIGN-V2.md). The v1 proof of concept
(LightRAG + Sourcebot + CodeGraphContext + Hindsight, verified end to end) lives on the `v1` branch.**

```sh
uv venv && uv pip install -e '.[all]'
cp cerebro.example.yaml cerebro.yaml
cerebro validate            # the one config file: identity, engines, provisioning, scopes
cerebro schema > cerebro.schema.json
pytest
```

Layout: `cerebro/core` (contracts, config, principal; imports no engine), `cerebro/gateway`, `cerebro/ingest`,
`cerebro/adapters/<kind>/<name>` (one per engine), `plugins/` (knowledge sources), `tests/contracts` (the harness
every adapter must pass). Details and the build order: [docs/DESIGN-V2.md](docs/DESIGN-V2.md).

Adapters on `main` so far (all tested against mocked engines; none verified against a live service yet):

| kind | `type:` | engine | unit it contributes |
|---|---|---|---|
| memory | `hindsight` | Hindsight 0.9.2 (`ghcr.io/vectorize-io/hindsight:0.9.2`), v1 wire calls, bank-addressed | `memory` (port 8888, secrets `HINDSIGHT_API_KEY`, `POSTGRES_PASSWORD`) |
| inference | `openai_compat` | any OpenAI `/v1` chat + embeddings server (`/v1` appended for provider ollama), key from `api_key_env` | none |
| inference | `ollama` | Ollama native `/api/chat`, `/api/embed`, `/api/tags` | none |

"""cerebro.bridge: the wire contract for stdio engines, served from inside a unit image.

    GET  /health
    GET  /.well-known/cerebro-capabilities   {engine, version, unit, capabilities: {...}, tools: [...], repos: [...]}
    POST /mcp                                 MCP over streamable HTTP, stateless, READ-ONLY tools only

The bridge spawns one stdio MCP engine (`cerebro.bridge.engine`), exposes an allowlisted subset of its tools with
`repo` / `branch` arguments in place of the engine's own root/branch selectors, and adds two tools of its own:
`grep` (ripgrep / git grep inside the workspace) and `unit_info` (the manifest). The workspace layout both the
bridge and the indexer follow is in `cerebro.bridge.workspace`.
"""

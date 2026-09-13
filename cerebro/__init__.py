"""cerebro: one MCP endpoint in front of documents, code intelligence and agent memory; every engine is an adapter.

Packages:
  cerebro.core        contracts, config schema, principal, adapter registry (imports no engine)
  cerebro.gateway     the MCP server: tools, identity, policy, fan-out (imports core only)
  cerebro.ingest      the sync engine that feeds DocumentIndex adapters from source plugins (imports core only)
  cerebro.adapters    one subpackage per contract kind; each adapter talks to its engine
  cerebro.sdk         the public SDK source plugins (plugins/*.py) import from
"""
__version__ = "2.0.0a0"

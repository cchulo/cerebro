"""cerebro.gateway: the MCP server. Imports cerebro.core only; every engine, identity source and policy is an
adapter built through cerebro.core.registry at startup.

    cerebro.gateway.server     Gateway: FastMCP tools, identity middleware, fan-out, activity log
    cerebro.gateway.identity   build_identity(config, ctx): identity.mode -> IdentityProvider (or a chain)
    cerebro.gateway.live       gateway side of the source plugins (live fallbacks)
    cerebro.gateway.cli        `cerebro gateway serve [-c cerebro.yaml]`
"""

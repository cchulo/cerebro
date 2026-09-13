"""cerebro.sdk: what a source plugin (plugins/*.py) imports. Stable; the v1 `stack_plugins` names, unchanged.

    from cerebro.sdk import Plugin, McpUpstream, Source, LiveSource, Document, ScopeContext, html_to_text, env
"""
from cerebro.core.contracts.sources import (Plugin, McpUpstream, Source, LiveSource, Document, ScopeContext,
                                            html_to_text, env, discover)

__all__ = ["Plugin", "McpUpstream", "Source", "LiveSource", "Document", "ScopeContext", "html_to_text", "env", "discover"]

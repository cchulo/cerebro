"""The contracts. One module per kind; adapters subclass exactly one of these per class."""
from .identity import RequestInfo, IdentityProvider, AuthorizationServer
from .policy import AccessPolicy
from .docs import DocumentText, Batch, ApplyReport, QueryOptions, Reference, DocAnswer, DocumentIndex
from .code import ToolInfo, Capabilities, ToolResult, SearchHit, CodeIntelligence
from .memory import Memory, RecallResult, RetainResult, ReflectResult, MemoryStore
from .inference import ChatMessage, Inference
from .provision import PortSpec, VolumeSpec, UnitSpec, JobSpec, UnitRef, Endpoint, UnitStatus, Provisioner
from .state import SyncState
from .sources import Document, ScopeContext, Source, LiveSource, McpUpstream, Plugin, discover, html_to_text

__all__ = [
    "RequestInfo", "IdentityProvider", "AuthorizationServer", "AccessPolicy",
    "DocumentText", "Batch", "ApplyReport", "QueryOptions", "Reference", "DocAnswer", "DocumentIndex",
    "ToolInfo", "Capabilities", "ToolResult", "SearchHit", "CodeIntelligence",
    "Memory", "RecallResult", "RetainResult", "ReflectResult", "MemoryStore",
    "ChatMessage", "Inference",
    "PortSpec", "VolumeSpec", "UnitSpec", "JobSpec", "UnitRef", "Endpoint", "UnitStatus", "Provisioner",
    "SyncState",
    "Document", "ScopeContext", "Source", "LiveSource", "McpUpstream", "Plugin", "discover", "html_to_text",
]

"""stack_plugins: the one framework every source plugin uses.

A plugin is ONE Python file in plugins/ that exposes a `PLUGIN = Plugin(...)` with up to three parts, all optional:

    source  a Source subclass       ingest side: list the documents a scope should contain (indexed into LightRAG)
    live    a LiveSource subclass   gateway side: search/read the system of record at query time, under the caller's scopes
    mcp     an McpUpstream          an MCP server image the stack runs for the live part (compose service / k8s pod)

The ingest loads `source`, the gateway loads `live`, the generator turns `mcp` into infrastructure, and
config/scopes.yaml references the plugin by `name` (a scope's `docs:` for ingest, the top-level `live:` for fallback).
Same file, same name, same credentials, same scope decision.
"""
from __future__ import annotations
import importlib.util, inspect, logging, os, pathlib, re, sys
from dataclasses import dataclass, field
from typing import Iterator

__all__ = ["Plugin", "McpUpstream", "Source", "Document", "ScopeContext", "LiveSource",
           "discover", "html_to_text", "env", "PLUGINS_DIR"]

log = logging.getLogger("stack_plugins")
PLUGINS_DIR = os.environ.get("PLUGINS_DIR", "/plugins")


# ----------------------------------------------------------------------------------------------- helpers
def env(key: str, default: str = "") -> str:
    """Environment variable (secrets, endpoints). Plugins read credentials from here, never from config files."""
    return os.environ.get(key, default)


def html_to_text(html: str) -> str:
    """HTML -> markdown when markdownify is available, else tag-stripped text. Use for any HTML-bodied source."""
    if not html:
        return ""
    try:
        from markdownify import markdownify
        return markdownify(html, heading_style="ATX")
    except ImportError:
        import html as _html
        text = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", html, flags=re.I)
        return _html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


# ----------------------------------------------------------------------------------------------- ingest side
@dataclass
class Document:
    key: str            # stable id within (plugin, scope), e.g. "ENG/12345", "docs/adr/0001.md"
    version: str        # changes iff text changes (revision number, updated-at, content hash)
    text: str           # markdown/plain text handed to LightRAG; prepend a small header with source + URL
    title: str = ""     # optional heading


@dataclass
class ScopeContext:
    scope: str
    config: dict        # this plugin's entry under the scope's `docs:` map, e.g. {"spaces": ["ENG"]}
    repos: list[str]    # the scope's code repositories (`code: { repos }`), for plugins that read docs out of repos


class _Configurable:
    def __init__(self, options: dict | None = None):
        self.options = options or {}    # the plugin's non-secret options from scopes.yaml (`sources:` / `live:`)

    def env(self, key: str, default: str = "") -> str:
        return env(key, default)

    def option(self, key: str, default=None):
        return self.options.get(key, default)

    def configured(self) -> bool:
        """False when credentials/endpoints are missing; the caller then skips this part with a note."""
        return True


class Source(_Configurable):
    """Ingest part. Rules: keys stable and unique within (plugin, scope); yield only what the WHOLE scope may read
    (skip anything with a finer ACL); never yield source code."""
    name: str = "base"

    def documents(self, ctx: ScopeContext, filter: dict | None = None) -> Iterator[Document]:
        raise NotImplementedError

    def covers(self, key: str, filter: dict | None) -> bool:
        """Which previously seen keys a filtered (webhook) run enumerates, so the engine can delete the vanished ones."""
        return filter is None or not filter


# ----------------------------------------------------------------------------------------------- gateway side
class LiveSource(_Configurable):
    """Fallback part. `allowed` = one entry per scope the caller may read that lists this plugin under `docs:`,
    e.g. [{"scope": "public", "spaces": ["ENG"]}]. Nothing outside `allowed` may ever leave search() or fetch();
    apply the same finer-ACL rule as the ingest. Talk REST, or an upstream MCP server (see McpUpstream)."""
    name: str = "base"

    async def search(self, query: str, allowed: list[dict], limit: int = 10) -> list[dict]:
        """[{"ref", "title", "scope", "url", "excerpt", ...}] inside `allowed` only."""
        raise NotImplementedError

    async def fetch(self, ref: str, allowed: list[dict], max_chars: int = 20000) -> dict:
        """{"ref", "title", "scope", "url", "text", ...} or raise PermissionError if outside `allowed`."""
        raise NotImplementedError


# ----------------------------------------------------------------------------------------------- infrastructure
@dataclass
class McpUpstream:
    """An MCP server image the stack runs next to the gateway for this plugin's live part.
    The generator emits a compose service / k8s Deployment+Service named `mcp-<plugin>`; the live part reaches it at
    http://mcp-<plugin>:<port><path> unless `live: <plugin>: { url: ... }` overrides."""
    image: str
    port: int = 9000
    path: str = "/mcp"
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)      # values may reference stack.env names as ${NAME}
    read_only: bool = True                                  # documentation only; enforce it through args/env too


@dataclass
class Plugin:
    name: str
    source: type[Source] | None = None
    live: type[LiveSource] | None = None
    mcp: McpUpstream | None = None
    description: str = ""

    def __post_init__(self):
        for part in (self.source, self.live):
            if part is not None and getattr(part, "name", "base") in ("base", None):
                part.name = self.name


# ----------------------------------------------------------------------------------------------- discovery
def discover(directory: str | os.PathLike | None = None) -> dict[str, Plugin]:
    """Import every *.py in the plugins directory and collect `PLUGIN` objects. A file without PLUGIN but with
    Source/LiveSource subclasses is wrapped automatically (name = the class's `name`)."""
    d = pathlib.Path(directory or PLUGINS_DIR)
    plugins: dict[str, Plugin] = {}
    if not d.is_dir():
        log.warning("plugins directory %s not found", d)
        return plugins
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))      # plugins may import sibling helper modules
    for f in sorted(d.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"plugins.{f.stem}", f)
            mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        except Exception:
            log.exception("plugin %s failed to import; skipped", f.name)
            continue
        found = []
        if isinstance(getattr(mod, "PLUGIN", None), Plugin):
            found.append(mod.PLUGIN)
        else:
            classes = [c for _, c in inspect.getmembers(mod, inspect.isclass) if c.__module__ == mod.__name__]
            for c in classes:
                if issubclass(c, Source) and c.name != "base":
                    found.append(Plugin(name=c.name, source=c))
                elif issubclass(c, LiveSource) and c.name != "base":
                    found.append(Plugin(name=c.name, live=c))
        for p in found:
            if p.name in plugins:
                log.warning("plugin '%s' from %s overrides an earlier definition", p.name, f.name)
            plugins[p.name] = p
            log.info("plugin %s: %s", p.name, ", ".join(k for k in ("source", "live", "mcp") if getattr(p, k)))
    return plugins

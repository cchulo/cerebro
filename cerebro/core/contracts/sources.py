"""Knowledge-source plugins: the v1 contract, unchanged in shape.

A plugin is ONE Python file in plugins/ that exposes `PLUGIN = Plugin(...)` with up to three parts, all optional:

    source  a Source subclass       ingest side: list the documents a scope should contain
    live    a LiveSource subclass   gateway side: search/read the system of record at query time, under the caller's scopes
    mcp     an McpUpstream          an MCP server image the stack runs for the live part (becomes a UnitSpec)

The ingest loads `source`, the gateway loads `live`, the provisioner turns `mcp` into a unit, and cerebro.yaml
references the plugin by `name` (a scope's `docs:` for ingest; the top-level `sources:` for options).
Plugins import this through `cerebro.sdk`.
"""
from __future__ import annotations
import importlib.util, inspect, logging, os, pathlib, re, sys
from dataclasses import dataclass, field
from typing import Iterator

log = logging.getLogger("cerebro.sources")


def env(key: str, default: str = "") -> str:
    """Environment variable (secrets, endpoints). Plugins read credentials from here, never from config files."""
    return os.environ.get(key, default)


def html_to_text(html: str) -> str:
    """HTML -> markdown when markdownify is available, else tag-stripped text."""
    if not html:
        return ""
    try:
        from markdownify import markdownify
        return markdownify(html, heading_style="ATX")
    except ImportError:
        import html as _html
        text = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", html, flags=re.I)
        return _html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


@dataclass
class Document:
    key: str            # stable id within (plugin, scope), e.g. "ENG/12345", "docs/adr/0001.md"
    version: str        # changes iff text changes (revision number, updated-at, content hash)
    text: str           # markdown/plain text; prepend a small header with source + URL
    title: str = ""


@dataclass
class ScopeContext:
    scope: str
    config: dict        # this plugin's entry under the scope's `docs:` map, e.g. {"spaces": ["ENG"]}
    repos: list[str]    # the scope's code repository URLs, for plugins that read docs out of repos


class _Configurable:
    def __init__(self, options: dict | None = None):
        self.options = options or {}    # the plugin's non-secret options (`sources:` in cerebro.yaml)

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


class LiveSource(_Configurable):
    """Fallback part. `allowed` = one entry per scope the caller may read that lists this plugin under `docs:`,
    e.g. [{"scope": "public", "spaces": ["ENG"]}]. Nothing outside `allowed` may ever leave search() or fetch()."""
    name: str = "base"

    async def search(self, query: str, allowed: list[dict], limit: int = 10) -> list[dict]:
        raise NotImplementedError

    async def fetch(self, ref: str, allowed: list[dict], max_chars: int = 20000) -> dict:
        raise NotImplementedError


@dataclass
class McpUpstream:
    """An MCP server image the stack runs next to the gateway for this plugin's live part, as unit `mcp-<plugin>`."""
    image: str
    port: int = 9000
    path: str = "/mcp"
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)      # values may reference secrets as ${NAME}
    read_only: bool = True


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


def discover(directory: str | os.PathLike | None = None) -> dict[str, Plugin]:
    """Import every *.py in the plugins directory and collect `PLUGIN` objects (or bare Source/LiveSource subclasses)."""
    d = pathlib.Path(directory or os.environ.get("CEREBRO_PLUGINS_DIR", "plugins"))
    plugins: dict[str, Plugin] = {}
    if not d.is_dir():
        log.warning("plugins directory %s not found", d)
        return plugins
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    for f in sorted(d.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"cerebro_plugins.{f.stem}", f)
            mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        except Exception:
            log.exception("plugin %s failed to import; skipped", f.name)
            continue
        found = []
        if isinstance(getattr(mod, "PLUGIN", None), Plugin):
            found.append(mod.PLUGIN)
        else:
            for _, c in inspect.getmembers(mod, inspect.isclass):
                if c.__module__ != mod.__name__:
                    continue
                if issubclass(c, Source) and c.name != "base":
                    found.append(Plugin(name=c.name, source=c))
                elif issubclass(c, LiveSource) and c.name != "base":
                    found.append(Plugin(name=c.name, live=c))
        for p in found:
            if p.name in plugins:
                log.warning("plugin '%s' from %s overrides an earlier definition", p.name, f.name)
            plugins[p.name] = p
    return plugins

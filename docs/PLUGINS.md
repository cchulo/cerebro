# Writing a plugin

Every system the stack reads is one plugin: a single Python file in `plugins/`, auto-discovered, no registration,
no image rebuild. Confluence, Backstage, git docs, local files and the JAMA example are written exactly the way
yours will be. They all do the same thing, so they share one framework, the `stack_plugins` SDK (`sdk/`), installed
in the ingest and gateway images.

## The shape

```python
from stack_plugins import Plugin, McpUpstream, Source, LiveSource, Document, ScopeContext, html_to_text

class JamaSource(Source):            # ingest part  (optional)
    ...
class JamaLive(LiveSource):          # fallback part (optional)
    ...
PLUGIN = Plugin(
    name="jama",
    source=JamaSource,
    live=JamaLive,
    mcp=McpUpstream(image="ghcr.io/example/mcp-jama:1.2.0", port=9000, path="/mcp",
                    args=["--transport", "streamable-http", "--port", "9000"],
                    env={"JAMA_URL": "${JAMA_URL}", "JAMA_TOKEN": "${JAMA_TOKEN}"}),   # optional
    description="Jama Connect requirements",
)
```

| Part | Runs in | Contract | Referenced by |
|---|---|---|---|
| `source` | ingest | `documents(ctx, filter) -> Iterator[Document]`; optional `covers(key, filter)` | a scope's `docs: { jama: {...} }` |
| `live` | gateway | `async search(query, allowed, limit) -> list[dict]`, `async fetch(ref, allowed, max_chars) -> dict` | on automatically; `live: { jama: {...} }` only overrides |
| `mcp` | infrastructure | image, port, path, args, env for an upstream MCP server | `make gen` emits `mcp-jama` (compose service / k8s Deployment) |

Any part may be omitted. A file that only defines a `Source` subclass with a `name` works too; `PLUGIN` is the
explicit form and the only way to declare `live` and `mcp`.

## The ingest part

```python
class JamaSource(Source):
    def __init__(self, options=None):
        super().__init__(options)
        self.url = (self.option("url") or self.env("JAMA_URL")).rstrip("/")   # option: sources: map; env: stack.env
        self.token = self.env("JAMA_TOKEN")

    def configured(self) -> bool:                # missing credentials -> the engine skips with a note
        return bool(self.url and self.token)

    def documents(self, ctx: ScopeContext, filter=None):
        for item in fetch_items(self.url, self.token, ctx.config["projects"]):   # ctx.config = the scope's docs: entry
            yield Document(key=f"{item.project}/{item.key}", version=item.modified, text=render(item), title=item.name)

    def covers(self, key, filter):                # which known keys a filtered (webhook) run enumerates
        return not filter or key.startswith(f"{filter['project']}/")
```

- `key` is stable and unique within (plugin, scope); it becomes part of the LightRAG source id. Changing the scheme
  re-ingests everything.
- `version` is any string that changes when the text changes: a revision number, an updated-at, a content hash.
- `text` is markdown or plain text; put a small header first (source, path, URL) so answers can cite it.
  `html_to_text()` converts HTML bodies.
- `ctx.repos` is the scope's `code.repos` for plugins that read documents out of repositories (the `git` plugin).
- Yield only what the whole scope may read. Anything with a finer ACL than the scope (a restricted page, a private
  item) is skipped, never yielded. Never yield source code.

The engine does the rest: version diffing, batching into the scope's LightRAG, deletion reconciling, state.

## The fallback part

```python
class JamaLive(LiveSource):
    async def search(self, query, allowed, limit=10):
        # allowed = [{"scope": "payments", "projects": [42]}, ...]  -> the callers' docs: entries for this plugin
        projects = [p for a in allowed for p in a.get("projects", [])]
        hits = await jama_search(query, projects)               # constrain the query with the allowed set
        return [{"ref": h.id, "title": h.name, "scope": scope_of(h.project, allowed), "url": h.url, "excerpt": h.text[:300]}
                for h in hits if h.project in projects]           # and re-check every result

    async def fetch(self, ref, allowed, max_chars=20000):
        item = await jama_get(ref)
        if item.project not in {p for a in allowed for p in a.get("projects", [])}:
            raise PermissionError(f"item {ref} is outside your scopes")
        return {"ref": ref, "title": item.name, "scope": ..., "url": item.url, "text": render(item)[:max_chars]}
```

The gateway calls `search` automatically when `query_docs` finds no answer in a scope's index (with the question
reduced to keywords), returns the hits under `fallback`, and serves `live_fetch` for one ref. Two rules make it safe:
nothing outside `allowed` ever leaves either method, and the same finer-ACL rule as the ingest applies. Inside, talk
REST, or an upstream MCP server; the Confluence plugin shows both (`plugins/confluence.py`).

## The MCP upstream

If the fallback should go through an existing MCP server (mcp-atlassian, a vendor's server, your own), declare its
image on the plugin. `make gen` turns it into `mcp-<plugin>` in `docker/compose.scopes.yaml` and
`k8s/generated/mcp-<plugin>.yaml`: no published port, reachable only inside the stack, `${NAME}` values in `env`
resolved from `config/stack.env`. The live part reaches it at `http://mcp-<plugin>:<port><path>` (or `url` from the
`live:` override). Prefer read-only flags and tool allowlists in `args`/`env` when the server offers them; the
gateway constrains scopes, but the upstream should not be able to write regardless.

Two things the framework does not do for you: it does not know which argument of the upstream's tools carries the
scope (your `search` builds that call), and most upstreams return no ACL metadata (the Confluence plugin cross-checks
page restrictions through REST for that reason). Design the live part around what the upstream can actually tell you.

## Configuration surface

| Where | What | Read with |
|---|---|---|
| `config/stack.env` | secrets and endpoints | `self.env("JAMA_TOKEN")` |
| `config/scopes.yaml` → `sources: { jama: {...} }` | non-secret ingest options (optional) | `self.option("page_size")` |
| `config/scopes.yaml` → `live: { jama: {...} }` | fallback overrides: `enabled`, `fallback`, `via`, `url`, `auth_env`, tool names (optional) | `self.option(...)` |
| `config/scopes.yaml` → scope → `docs: { jama: {...} }` | what of this source belongs to the scope | `ctx.config` / `allowed[i]` |

## Developing and testing

```sh
make source-check SCOPE=payments SOURCE=jama                 # lists what the ingest part yields, no LightRAG
make source-check SCOPE=payments SOURCE=jama ARGS="--filter '{\"project\": 42}' --limit 5 --full"
make gen && make up                                          # brings up mcp-jama if declared
curl localhost:8080/sources                                  # discovered plugins and whether they are configured
```

`make smoke ARGS=--live` runs the isolation checks; add markers to your fixtures the same way `test/fixtures` does
for Confluence if you want your plugin covered. Plugins are imported on the host by `make gen` too, so keep their
top-level imports to `httpx`, `markdownify` and the standard library (or import heavier dependencies inside methods).

## Packaging instead of a file

For a plugin you version and test separately, ship it as a package with an entry point and install it in the
images: `[project.entry-points."context_stack.sources"] jama = "acme_sources.jama:JamaSource"` (ingest part). The
file-in-`plugins/` form remains the simplest and is what the shipped plugins use.

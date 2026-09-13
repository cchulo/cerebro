# Writing a plugin

Every system the stack reads is one plugin: a single Python file in `plugins/` (or `gateway.plugins_dir`),
auto-discovered, no registration, no image rebuild. Confluence, Backstage, git docs, local files and the Jama example
are written exactly the way yours will be. They share one framework, `cerebro.sdk` (the v1 `stack_plugins` names,
unchanged; that module name still imports), which the ingest and gateway images carry.

## The shape

```python
from cerebro.sdk import Plugin, McpUpstream, Source, LiveSource, Document, ScopeContext, html_to_text

class JamaSource(Source):            # ingest part  (optional)
    ...
class JamaLive(LiveSource):          # live part    (optional)
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
| `source` | ingest | `documents(ctx, filter) -> Iterator[Document]`; optional `covers(key, filter)`, `configured()` | a scope's `docs: { jama: {...} }` |
| `live` | gateway | `async search(query, allowed, limit) -> list[dict]`, `async fetch(ref, allowed, max_chars) -> dict` | on by default for every plugin that has one; `sources.jama.live` overrides |
| `mcp` | provisioner | image, port, path, args, env of an upstream MCP server | becomes unit `mcp-jama` in the plan |

Any part may be omitted. A file that only defines a `Source` subclass with a `name` works too; `PLUGIN` is the explicit
form and the only way to declare `live` and `mcp`.

## The ingest part

```python
class JamaSource(Source):
    name = "jama"

    def __init__(self, options=None):
        super().__init__(options)
        self.url = (self.option("url") or self.env("JAMA_URL")).rstrip("/")   # option: sources.jama; env: secrets.env
        self.token = self.env("JAMA_TOKEN")

    def configured(self) -> bool:                # missing credentials -> the engine skips with a note
        return bool(self.url and self.token)

    def documents(self, ctx: ScopeContext, filter=None):
        for item in fetch_items(self.url, self.token, ctx.config["projects"]):   # ctx.config = the scope's docs: entry
            yield Document(key=f"{item.project}/{item.key}", version=item.modified, text=render(item), title=item.name)

    def covers(self, key, filter):                # which known keys a filtered (webhook) run enumerates
        return not filter or key.startswith(f"{filter['project']}/")
```

- `key` is stable and unique within (plugin, scope). The index source id is `<plugin>:<scope>:<key>`; changing the
  scheme re-ingests everything.
- `version` is any string that changes when the text changes: a revision number, an updated-at, a content hash.
- `text` is markdown or plain text; put a small header first (source, path, URL) so answers can cite it.
  `html_to_text()` converts HTML bodies.
- `ctx.repos` is the scope's `code.repos` URLs, for plugins that read documents out of repositories (the `git` plugin).
- Yield only what the whole scope may read. Anything with a finer ACL than the scope (a restricted page, a private
  item) is skipped, never yielded. Never yield source code.

The engine does the rest: version diffing against `SyncState`, one `Batch` per scope handed to the `DocumentIndex`
adapter (which decides ordering and idle waits), deletion reconciling through `covers()`, and a state commit only
after the index accepted the batch.

## The live part

```python
class JamaLive(LiveSource):
    async def search(self, query, allowed, limit=10):
        # allowed = [{"scope": "payments", "projects": [42]}, ...]: the caller's docs: entries for this plugin
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
REST or an upstream MCP server; the Confluence plugin shows both (`plugins/confluence.py`).

## The MCP upstream

If the live part should go through an existing MCP server (mcp-atlassian, a vendor's server, your own), declare its
image on the plugin. The planner turns it into unit `mcp-<plugin>` (role `mcp`, no published port, reachable only on
the stack network at `http://mcp-<plugin>:<port><path>`); `${NAME}` values in `env` become secret references resolved
at run time, so add the names to `secrets.keys`. Prefer read-only flags and tool allowlists in `args` / `env`.

The unit is planned for every plugin with both `mcp` and `live`, whether or not a scope uses the plugin, unless
`cerebro.yaml` says otherwise:

```yaml
sources:
  confluence: { live: { enabled: false } }        # no live part, no unit
  confluence: { live: { via: rest } }             # the live part talks REST itself, no unit
  confluence: { live: { url: http://... } }       # an upstream that already runs elsewhere, no unit
```

Two things the framework does not do for you: it does not know which argument of the upstream's tools carries the
scope (your `search` builds that call), and most upstreams return no ACL metadata (the Confluence plugin cross-checks
page restrictions through REST for that reason).

## Configuration surface

| Where | What | Read with |
|---|---|---|
| `secrets.env` (compose) / the `cerebro-secrets` Secret (kubernetes) | credentials and endpoints; names listed in `secrets.keys` | `self.env("JAMA_TOKEN")` |
| `cerebro.yaml` `sources: { jama: {...} }` | non-secret options shared by both parts | `self.option("page_size")` |
| `cerebro.yaml` `sources: { jama: { live: {...} } }` | live overrides: `enabled`, `fallback` (keep out of the automatic fallback but still callable), `via`, `url`, `auth_env`, tool names, `type: module:Class` to swap the class | `self.option(...)` (everything except `enabled` / `fallback`) |
| `cerebro.yaml` scope `docs: { jama: {...} }` | what of this source belongs to the scope | `ctx.config` / `allowed[i]` |

## Developing and testing

```sh
cerebro ingest -c cerebro.yaml check payments jama                    # what the ingest part yields, no index
cerebro ingest -c cerebro.yaml check payments jama --filter '{"project": 42}' --limit 5 --full
cerebro ingest -c cerebro.yaml sync jama --scope payments             # one plugin, one scope, report as JSON
cerebro provision plan                                                # mcp-jama appears when declared
curl -s http://ingest:8080/sources                                    # inside the stack: plugins and whether configured
```

`check` prints the available plugins first, then `configured=`, the options and the scope entry, then each document's
key, version, title and a preview; exit code 1 for an unknown scope or source, 2 when the plugin is not configured.
Plugins are imported by the gateway, the ingest and the planner (on the host too), so keep top-level imports to
`httpx`, `markdownify` and the standard library, or import heavier dependencies inside methods. On Kubernetes the
files are copied into the `cerebro-plugins` ConfigMap at render time.

## Packaging instead of a file

Out-of-tree engines use `type: mypkg.module:Class` in `cerebro.yaml`; plugins stay files in `plugins/`, which is what
the shipped ones use. A plugin you version separately can still be a one-file wrapper that imports your package.

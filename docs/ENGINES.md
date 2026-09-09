# The engines: what each one is for, and what it is not

Questions that come up when people first read the stack. Short answers first, detail below.

| Question | Answer |
|---|---|
| Why two code tools? | Sourcebot answers *where is this text*, CodeGraphContext answers *how is this connected*. Neither can do the other's job. |
| Is "codegraph" a second product? | No. `codegraph-<scope>`, `mcp/codegraph-mcp/` and the `code_graph` tool are our handles for **CodeGraphContext**. FalkorDB's separate *CodeGraph* project is not in the stack. |
| Why not put the docs into Hindsight? | Hindsight is agent memory (what happened), not a document store (what the docs say). Mixing them ruins both. |
| Why one LightRAG per scope instead of one big one? | A graph index merges knowledge across documents at index time; filtering results afterwards leaks. The boundary has to be the index. |
| What is FalkorDB doing there? | It is only the database CodeGraphContext writes its graph into. Nothing talks to it directly except CodeGraphContext and the indexer job. |
| Which engines call the LLM? | LightRAG and Hindsight. Sourcebot and CodeGraphContext use no model. |
| Are documents fetched when I query? | No. The ingest pulls on webhooks, a schedule or on demand and only changed documents are re-ingested; queries read the index. See [SOURCES.md](SOURCES.md#freshness-when-documents-are-pulled). |
| Anything not truly open source? | Sourcebot (FSL-1.1, source-available) and FalkorDB (SSPL v1, not OSI-approved). Everything else is MIT / Apache / BSD / PostgreSQL. |

## Code search vs code graph

**Sourcebot** is a code search engine built on Zoekt, the trigram indexer behind Sourcegraph. Exact, regex and
symbol search across every repo at once, in milliseconds, with no understanding of the code. That is exactly why
it is good at what a graph cannot do: a string literal, a config key, an error message, a TODO, a Dockerfile line,
a file in a language the parser does not know, and always with file paths and line numbers. Sourcebot also syncs
the repositories itself from GitHub/GitLab on a schedule, so it is the one component that always has current code,
and it has a web UI developers use directly. The gateway exposes it as `search_code`, appends a `repo:` filter for
the caller's repositories, and drops anything outside that list a second time on the way back. It indexes the
default branch unless a connection lists `revisions` (branches/tags, globs, max 64 each); `rev:<branch>` then
narrows a query. CodeGraphContext and the git docs adapter see only the default branch.

**CodeGraphContext** parses repositories with tree-sitter into a graph of files, classes and functions with
call / import / inheritance edges, stores it in FalkorDB and exposes it over MCP. It answers structural questions:
who calls this, what breaks if I change it, class hierarchy, dead code, complexity. It cannot find a string, and it
knows only what the per-scope indexer job has parsed. The gateway exposes a read-only allowlist of its tools as
`code_graph`, one scope per call.

A typical agent flow uses both: `search_code("handlePayment")` to locate the function, then
`code_graph(scope, "analyze_code_relationships", {query_type: "find_callers", target: "handlePayment"})` before
changing its signature.

## The "CodeGraph" name collision

Three different things share the word:

1. **A code graph**, the concept: nodes are files/classes/functions, edges are calls/imports/inheritance. Any tool that
   builds one (Sourcegraph's SCIP index, GitHub's, ours) "has a code graph". Our docs use "code graph layer" in this sense.
2. **CodeGraphContext** (`cgc`, MIT), the tool we run: broad language coverage, an indexing CLI we build the indexer
   job on, remote FalkorDB support (one per scope), SCIP import, MCP server. This is the only graph tool in the stack.
3. **CodeGraph by FalkorDB** (`FalkorDB/code-graph`, MIT): a different project from the makers of the database we
   use, a web UI + REST API for visualising a codebase, with an MCP server added later. Its analyzers cover Python,
   Java and C# only. Not used here; it is a candidate for the pilot comparison next to GitNexus if language coverage
   is acceptable for the org.

Naming inside this repo: `codegraph-<scope>` (container/Service), `mcp/codegraph-mcp/` (image), `code_graph`
(gateway tool). One product, three handles.

## Memory vs documents

**Hindsight** is agent memory. It receives what agents `retain`: outcomes, decisions, corrections, things the docs
got wrong. It extracts facts with the LLM, links entities, consolidates over time, and answers `recall` and
`reflect`. Banks are `user-<id>` (private) and `team-<group>` (shared with that IdP group). It is deliberately not
fed Confluence or code: a memory engine learning from thousands of pages produces confident, stale, unattributed
answers and loses the point of memory, which is *what we learned by doing*.

**LightRAG** is the document knowledge graph, one instance per scope. The ingest feeds it Confluence pages,
Backstage entities, repo docs and files through adapters; it extracts entities and relations with the LLM and
answers `query_docs` with references back to the source ids. It never receives code (that is Sourcebot's and
CodeGraphContext's job) and never receives agent memories.

Where they meet: an agent reads docs and code, does the work, and `retain`s the outcome. If the runbook was wrong,
that fact lives in memory until someone fixes the runbook, at which point the next ingest updates the docs graph.

## Why per-scope instances

Graph indexes (LightRAG's entity graph, CodeGraphContext's call graph) merge information across their inputs at
index time. A restricted page and a public one contribute to the same entity node; a call edge crosses repository
boundaries. Filtering *results* per user afterwards cannot undo that merge, so the only safe boundary is the index
itself: every scope gets its own LightRAG and its own FalkorDB + CodeGraphContext. Sourcebot does not merge anything
(each match is one file in one repo), so a single instance with a per-query repository filter is safe. Hindsight
partitions by bank. Details and the cost of this in [ACCESS-CONTROL.md](ACCESS-CONTROL.md).

## Licensing and fallbacks

| Component | Licence | If it becomes a problem |
|---|---|---|
| Sourcebot | FSL-1.1 (source-available; converts to Apache 2.0 two years after each release) | Run plain **Zoekt** (Apache 2.0): same search engine, no UI/sync/API-key layer; `search_code` would target Zoekt's API and repos would be synced by a small job. Needs legal sign-off before Sourcebot is load-bearing. |
| CodeGraphContext | MIT | **GitNexus** is technically stronger but PolyForm-Noncommercial: pilot comparison only. SCIP indexers (Apache 2.0) can feed CodeGraphContext directly. |
| LightRAG | MIT | LazyGraphRAG if global-synthesis queries fall short; same ingest adapters. |
| Hindsight | MIT | — |
| FalkorDB | SSPL v1 (server; not OSI-approved) | Only reachable from CodeGraphContext inside the cluster; CodeGraphContext also supports Neo4j and embedded stores. |
| Postgres + pgvector, Redis, Ollama | PostgreSQL / BSD / MIT | — |

Check the exact licence text of the pinned versions (README "Verified versions") before production; the table
records the state on 2026-09-09.

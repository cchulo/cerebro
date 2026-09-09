# Architecture

One MCP endpoint per developer; behind it three engines, isolated per scope; one shared inference backend that is the
only place text can leave the stack.

## System

```mermaid
%%{init: {
  "theme": "base",
  "themeVariables": {
    "fontFamily": "system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif",
    "fontSize": "14px",
    "background": "#0b1220",
    "primaryColor": "#334155",
    "primaryTextColor": "#ffffff",
    "primaryBorderColor": "#94a3b8",
    "secondaryColor": "#334155",
    "tertiaryColor": "#1f2937",
    "textColor": "#f8fafc",
    "titleColor": "#f8fafc",
    "lineColor": "#cbd5e1",
    "clusterBkg": "#1f2937",
    "clusterBorder": "#64748b",
    "edgeLabelBackground": "#1e293b",
    "nodeTextColor": "#ffffff"
  },
  "flowchart": { "curve": "basis", "nodeSpacing": 30, "rankSpacing": 55, "padding": 12 }
}}%%
flowchart LR
    classDef client fill:#0369a1,stroke:#7dd3fc,stroke-width:2px,color:#ffffff
    classDef edge fill:#475569,stroke:#cbd5e1,stroke-width:2px,color:#ffffff
    classDef gateway fill:#2563eb,stroke:#bfdbfe,stroke-width:2.5px,color:#ffffff
    classDef docs fill:#7c3aed,stroke:#ddd6fe,stroke-width:2px,color:#ffffff
    classDef code fill:#059669,stroke:#a7f3d0,stroke-width:2px,color:#ffffff
    classDef memory fill:#d97706,stroke:#fde68a,stroke-width:2px,color:#ffffff
    classDef infra fill:#64748b,stroke:#e2e8f0,stroke-width:2px,color:#ffffff
    classDef job fill:#475569,stroke:#e2e8f0,stroke-width:2px,stroke-dasharray:4 3,color:#ffffff
    classDef external fill:#374151,stroke:#d1d5db,stroke-width:2px,stroke-dasharray:5 4,color:#f9fafb
    classDef model fill:#e11d48,stroke:#fecdd3,stroke-width:2.5px,color:#ffffff

    subgraph DEV[" Developers "]
        direction TB
        CC["Claude Code"]:::client
        CU["Cursor / other MCP clients"]:::client
    end

    subgraph EDGE[" SSO edge "]
        PROXY["Reverse proxy + OIDC<br/><small>sets X-Forwarded-User / -Groups</small>"]:::edge
    end

    subgraph STACK[" Self-hosted boundary (Compose or k3s) "]
        direction LR
        GW["<b>MCP gateway</b><br/><small>list_scopes · query_docs · search_code<br/>code_graph · recall · retain · reflect</small>"]:::gateway

        subgraph SCOPE[" Scope: one per IdP group (×N) "]
            direction TB
            LRAG["LightRAG<br/><small>documents graph</small>"]:::docs
            CG["CodeGraphContext<br/><small>MCP over HTTP</small>"]:::code
            FK[("FalkorDB<br/><small>code graph</small>")]:::code
            CG --- FK
        end

        subgraph SHARED[" Shared engines "]
            direction TB
            SB["Sourcebot<br/><small>code search (Zoekt)</small>"]:::code
            HS["Hindsight<br/><small>agent memory · banks per user / team</small>"]:::memory
        end

        subgraph PIPE[" Pipelines "]
            direction TB
            ING["Ingest service<br/><small>adapters: confluence · backstage<br/>git · files · custom</small>"]:::job
            IDX["Indexer job per scope<br/><small>clone + cgc index</small>"]:::job
        end

        subgraph INFRA[" Shared infrastructure "]
            direction TB
            PG[("Postgres + pgvector<br/><small>db per engine</small>")]:::infra
            RD[("Redis")]:::infra
        end
    end

    subgraph MODEL[" Inference backend — the only place text leaves the stack "]
        LLM["Ollama (local)<br/><i>or</i><br/>org-approved OpenAI-compatible endpoint"]:::model
    end

    subgraph SRC[" Your systems (read-only) "]
        direction TB
        CONF["Confluence"]:::external
        BS["Backstage"]:::external
        GIT["Code host<br/><small>GitHub / GitLab</small>"]:::external
    end

    CC & CU --> PROXY --> GW
    GW -->|"scopes of caller"| LRAG
    GW -->|"scopes of caller"| CG
    GW -->|"repo filter"| SB
    GW -->|"caller's banks"| HS

    CONF & BS --> ING
    GIT --> ING
    GIT --> IDX
    GIT --> SB
    ING -->|"batched upserts"| LRAG
    IDX --> FK

    LRAG -.->|"extraction · answers · embeddings"| LLM
    HS -.-> LLM
    LRAG & HS & SB --> PG
    SB --> RD

    style STACK fill:#1e293b,stroke:#94a3b8,stroke-width:2.5px,color:#f8fafc
    style SCOPE fill:#312e81,stroke:#c4b5fd,stroke-width:2px,color:#f8fafc
    style SHARED fill:#065f46,stroke:#6ee7b7,stroke-width:2px,color:#f8fafc
    style PIPE fill:#334155,stroke:#94a3b8,stroke-width:2px,color:#f8fafc
    style INFRA fill:#334155,stroke:#94a3b8,stroke-width:2px,color:#f8fafc
    style MODEL fill:#881337,stroke:#fda4af,stroke-width:2.5px,stroke-dasharray:6 4,color:#fff1f2
    style DEV fill:#0c4a6e,stroke:#7dd3fc,stroke-width:2px,color:#f0f9ff
    style EDGE fill:#334155,stroke:#cbd5e1,stroke-width:2px,color:#f8fafc
    style SRC fill:#1f2937,stroke:#9ca3af,stroke-width:2px,stroke-dasharray:5 4,color:#f9fafb
```

**Reading it**

- **Blue** is the only thing agents talk to. The gateway computes the caller's scopes from the proxy's identity
  headers and never lets a request pick an engine, a scope or a memory bank it is not entitled to.
- **Purple / green** are duplicated per scope: a document graph and a code graph merge knowledge across their
  inputs, so the access boundary has to be the index itself, not a filter on results.
- **Amber** is shared but partitioned by bank: `user-<id>` private, `team-<group>` shared with that IdP group.
- **Red** is the one outbound dependency. Only LightRAG and Hindsight send text there; Sourcebot and
  CodeGraphContext use no model. Point it at local Ollama or at an endpoint your organisation controls.
- **Dashed** boxes are read-only sources; nothing is ever written back to them.
- **Naming**: CodeGraphContext is the product (`cgc`). `codegraph-<scope>` is our container/Service running it,
  `mcp/codegraph-mcp/` its image, and `code_graph` the gateway tool that proxies it. One thing, three handles.

Colors are set explicitly (dark palette), so the diagrams look the same on GitHub light and dark themes.

## One task, end to end

```mermaid
%%{init: {
  "theme": "base",
  "themeVariables": {
    "fontFamily": "system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif",
    "fontSize": "13px",
    "background": "#0b1220",
    "textColor": "#e2e8f0",
    "actorBkg": "#2563eb", "actorBorder": "#bfdbfe", "actorTextColor": "#ffffff",
    "actorLineColor": "#94a3b8",
    "signalColor": "#e2e8f0", "signalTextColor": "#ffffff",
    "labelBoxBkgColor": "#334155", "labelBoxBorderColor": "#94a3b8", "labelTextColor": "#f8fafc",
    "loopTextColor": "#f8fafc",
    "noteBkgColor": "#d97706", "noteBorderColor": "#fde68a", "noteTextColor": "#ffffff",
    "activationBkgColor": "#1d4ed8", "activationBorderColor": "#93c5fd",
    "sequenceNumberColor": "#0b1220"
  },
  "sequence": { "mirrorActors": false, "actorMargin": 40, "messageMargin": 34, "boxMargin": 8 }
}}%%
sequenceDiagram
    autonumber
    participant A as Agent
    participant P as SSO proxy
    participant G as Gateway
    participant H as Hindsight
    participant L as LightRAG (scope)
    participant S as Sourcebot
    participant C as CodeGraphContext (scope)
    participant M as Inference backend

    A->>P: MCP over HTTPS (session cookie / token)
    P->>G: + X-Forwarded-User, X-Forwarded-Groups
    Note over G: groups → scopes, banks<br/>(config/scopes.yaml)

    A->>G: recall("prior work on checkout deploys")
    G->>H: POST /banks/user-alice/memories/recall
    H->>M: embed query
    H-->>G: memories
    G-->>A: what was tried, decisions, corrections

    A->>G: query_docs("how do we deploy checkout?")
    G->>L: POST /query — only the caller's scopes
    L->>M: retrieve + answer
    L-->>G: answer + references
    G-->>A: answer with source ids

    A->>G: search_code / code_graph
    G->>S: query + repo:^…$ filter for the caller's repos
    G->>C: read-only tool inside one allowed scope
    S-->>G: matches (post-filtered again)
    C-->>G: callers, blast radius
    G-->>A: results

    Note over A: does the work

    A->>G: retain("deployed 2.3.1, runbook rollback flag was wrong")
    G->>H: POST /banks/user-alice/memories (async)
    H->>M: extract facts, embed
    G-->>A: operation id
```

The identity headers are the whole trust model: they exist only because the proxy is the single route to the
gateway. Everything downstream carries service credentials that clients never see (LightRAG API key, Hindsight
tenant key, Sourcebot API key).

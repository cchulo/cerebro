# Architecture

One MCP endpoint per developer; behind it three engines, isolated per scope; one shared inference backend that is the
only place text can leave the stack.

## System

```mermaid
%%{init: {
  "theme": "base",
  "themeVariables": {
    "fontFamily": "Inter, ui-sans-serif, system-ui, sans-serif",
    "fontSize": "14px",
    "primaryColor": "#1e293b",
    "primaryTextColor": "#f8fafc",
    "primaryBorderColor": "#334155",
    "lineColor": "#64748b",
    "clusterBkg": "#f8fafc",
    "clusterBorder": "#cbd5e1",
    "edgeLabelBackground": "#ffffff",
    "tertiaryColor": "#f1f5f9"
  },
  "flowchart": { "curve": "basis", "nodeSpacing": 30, "rankSpacing": 55, "padding": 12 }
}}%%
flowchart LR
    classDef client fill:#0f172a,stroke:#38bdf8,stroke-width:1.5px,color:#f8fafc
    classDef edge fill:#0f172a,stroke:#94a3b8,stroke-width:1.5px,color:#f8fafc
    classDef gateway fill:#1d4ed8,stroke:#93c5fd,stroke-width:2px,color:#ffffff
    classDef docs fill:#6d28d9,stroke:#c4b5fd,stroke-width:1.5px,color:#ffffff
    classDef code fill:#047857,stroke:#6ee7b7,stroke-width:1.5px,color:#ffffff
    classDef memory fill:#b45309,stroke:#fcd34d,stroke-width:1.5px,color:#ffffff
    classDef infra fill:#475569,stroke:#94a3b8,stroke-width:1.5px,color:#f8fafc
    classDef job fill:#334155,stroke:#94a3b8,stroke-width:1.5px,stroke-dasharray:4 3,color:#f8fafc
    classDef external fill:#ffffff,stroke:#94a3b8,stroke-width:1.5px,stroke-dasharray:5 4,color:#334155
    classDef model fill:#be123c,stroke:#fda4af,stroke-width:2px,color:#ffffff

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

    LRAG & HS -.->|"extraction · answers · embeddings"| LLM
    LRAG & HS & SB --> PG
    SB --> RD

    style STACK fill:#f8fafc,stroke:#64748b,stroke-width:2px
    style SCOPE fill:#ede9fe,stroke:#a78bfa,stroke-width:1.5px
    style SHARED fill:#ecfdf5,stroke:#6ee7b7,stroke-width:1.5px
    style PIPE fill:#f1f5f9,stroke:#cbd5e1,stroke-width:1.5px
    style INFRA fill:#f1f5f9,stroke:#cbd5e1,stroke-width:1.5px
    style MODEL fill:#fff1f2,stroke:#fb7185,stroke-width:2px,stroke-dasharray:6 4
    style DEV fill:#f0f9ff,stroke:#7dd3fc,stroke-width:1.5px
    style EDGE fill:#f8fafc,stroke:#94a3b8,stroke-width:1.5px
    style SRC fill:#ffffff,stroke:#cbd5e1,stroke-width:1.5px,stroke-dasharray:5 4
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

## One task, end to end

```mermaid
%%{init: {
  "theme": "base",
  "themeVariables": {
    "fontFamily": "Inter, ui-sans-serif, system-ui, sans-serif",
    "fontSize": "13px",
    "actorBkg": "#1e293b", "actorBorder": "#334155", "actorTextColor": "#f8fafc",
    "actorLineColor": "#94a3b8",
    "signalColor": "#334155", "signalTextColor": "#1e293b",
    "noteBkgColor": "#fef3c7", "noteBorderColor": "#f59e0b", "noteTextColor": "#78350f",
    "activationBkgColor": "#dbeafe", "activationBorderColor": "#3b82f6",
    "sequenceNumberColor": "#ffffff"
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
    participant C as CodeGraph (scope)
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

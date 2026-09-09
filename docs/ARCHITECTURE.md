# Architecture

One MCP endpoint per developer; behind it three engines, isolated per scope; one shared inference backend that is the
only place text can leave the stack.

> **Viewing tip.** Each diagram is also rendered as an SVG you can open on its own and zoom without limit:
> [img/system.svg](img/system.svg) and [img/task-flow.svg](img/task-flow.svg). Inline, GitHub shows a zoom/full-screen
> toolbar when you hover a diagram; VS Code's preview needs the *Markdown Preview Mermaid Support* extension and only
> scales with the whole preview (Cmd/Ctrl +). Regenerate the SVGs after editing a diagram: `make diagrams`.

## System

[![System architecture](img/system.svg)](img/system.svg)

<details><summary>Mermaid source</summary>

```mermaid
%%{init: {
  "theme": "base",
  "themeVariables": {
    "fontFamily": "system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif",
    "fontSize": "14px",
    "background": "#0b1220",
    "primaryColor": "#1e293b",
    "primaryTextColor": "#f1f5f9",
    "primaryBorderColor": "#475569",
    "secondaryColor": "#1e293b",
    "tertiaryColor": "#111827",
    "textColor": "#e2e8f0",
    "titleColor": "#e2e8f0",
    "lineColor": "#94a3b8",
    "clusterBkg": "#111827",
    "clusterBorder": "#334155",
    "edgeLabelBackground": "#0b1220",
    "nodeTextColor": "#f1f5f9"
  },
  "flowchart": { "curve": "basis", "nodeSpacing": 30, "rankSpacing": 55, "padding": 12 }
}}%%
flowchart LR
    classDef client fill:#0c4a6e,stroke:#38bdf8,stroke-width:1.5px,color:#f0f9ff
    classDef edge fill:#1e293b,stroke:#94a3b8,stroke-width:1.5px,color:#f1f5f9
    classDef gateway fill:#1d4ed8,stroke:#93c5fd,stroke-width:2px,color:#ffffff
    classDef docs fill:#6d28d9,stroke:#c4b5fd,stroke-width:1.5px,color:#ffffff
    classDef code fill:#047857,stroke:#6ee7b7,stroke-width:1.5px,color:#ffffff
    classDef memory fill:#b45309,stroke:#fcd34d,stroke-width:1.5px,color:#ffffff
    classDef infra fill:#334155,stroke:#94a3b8,stroke-width:1.5px,color:#f1f5f9
    classDef job fill:#1e293b,stroke:#94a3b8,stroke-width:1.5px,stroke-dasharray:4 3,color:#f1f5f9
    classDef external fill:#111827,stroke:#94a3b8,stroke-width:1.5px,stroke-dasharray:5 4,color:#cbd5e1
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

    LRAG -.->|"extraction · answers · embeddings"| LLM
    HS -.-> LLM
    LRAG & HS & SB --> PG
    SB --> RD

    style STACK fill:#0f172a,stroke:#64748b,stroke-width:2px,color:#e2e8f0
    style SCOPE fill:#1e1b4b,stroke:#a78bfa,stroke-width:1.5px,color:#e2e8f0
    style SHARED fill:#052e2b,stroke:#34d399,stroke-width:1.5px,color:#e2e8f0
    style PIPE fill:#111827,stroke:#475569,stroke-width:1.5px,color:#e2e8f0
    style INFRA fill:#111827,stroke:#475569,stroke-width:1.5px,color:#e2e8f0
    style MODEL fill:#2a0a12,stroke:#fb7185,stroke-width:2px,stroke-dasharray:6 4,color:#fecdd3
    style DEV fill:#082f49,stroke:#38bdf8,stroke-width:1.5px,color:#e0f2fe
    style EDGE fill:#111827,stroke:#94a3b8,stroke-width:1.5px,color:#e2e8f0
    style SRC fill:#0b1220,stroke:#64748b,stroke-width:1.5px,stroke-dasharray:5 4,color:#cbd5e1
```

</details>

**Reading it**

- **Blue** is the only thing agents talk to. The gateway computes the caller's scopes from the proxy's identity
  headers and never lets a request pick an engine, a scope or a memory bank it is not entitled to.
- **Purple / green** are duplicated per scope: a document graph and a code graph merge knowledge across their
  inputs, so the access boundary has to be the index itself, not a filter on results.
- **Amber** is shared but partitioned by bank: `user-<id>` private, `team-<group>` shared with that IdP group.
- **Red** is the one outbound dependency. Only LightRAG and Hindsight send text there; Sourcebot and
  CodeGraphContext use no model. Point it at local Ollama or at an endpoint your organisation controls.
- **Dashed** boxes are read-only sources; nothing is ever written back to them.

Colors are set explicitly (dark palette), so the diagrams look the same on GitHub light and dark themes.

## One task, end to end

[![One task end to end](img/task-flow.svg)](img/task-flow.svg)

<details><summary>Mermaid source</summary>

```mermaid
%%{init: {
  "theme": "base",
  "themeVariables": {
    "fontFamily": "system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif",
    "fontSize": "13px",
    "background": "#0b1220",
    "textColor": "#e2e8f0",
    "actorBkg": "#1e293b", "actorBorder": "#64748b", "actorTextColor": "#f1f5f9",
    "actorLineColor": "#475569",
    "signalColor": "#94a3b8", "signalTextColor": "#e2e8f0",
    "labelBoxBkgColor": "#1e293b", "labelBoxBorderColor": "#475569", "labelTextColor": "#e2e8f0",
    "loopTextColor": "#e2e8f0",
    "noteBkgColor": "#3b2a06", "noteBorderColor": "#f59e0b", "noteTextColor": "#fde68a",
    "activationBkgColor": "#1e3a8a", "activationBorderColor": "#60a5fa",
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

</details>

The identity headers are the whole trust model: they exist only because the proxy is the single route to the
gateway. Everything downstream carries service credentials that clients never see (LightRAG API key, Hindsight
tenant key, Sourcebot API key).

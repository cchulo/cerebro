#!/usr/bin/env python3
"""Generate per-scope services from config/scopes.yaml.

  scripts/gen-scopes.py compose  > docker/compose.scopes.yaml
  scripts/gen-scopes.py k8s      # writes k8s/generated/ (per-scope manifests, Secret + ConfigMaps from config/)

Per scope: lightrag-<scope> (docs graph), falkordb-<scope> + codegraph-<scope> (code graph), indexer-<scope> (job).
Service/container names are identical in compose and Kubernetes, so the gateway and ingest need no changes.
"""
import re, shutil, sys, yaml, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "sdk"))
import stack_plugins

cfg = yaml.safe_load(open(pathlib.Path(__file__).parent.parent / "config/scopes.yaml"))
scopes = cfg["scopes"]
LIVE_OVERRIDES = {k: (v or {}) for k, v in (cfg.get("live") or {}).items()}


def mcp_upstreams() -> dict[str, "stack_plugins.McpUpstream"]:
    """Plugins whose live part is enabled and whose transport is the declared MCP upstream -> image to run."""
    out = {}
    for name, plugin in stack_plugins.discover(pathlib.Path(__file__).parent.parent / "plugins").items():
        o = LIVE_OVERRIDES.get(name, {})
        if plugin.mcp and plugin.live and o.get("enabled", True) is not False and o.get("via", "mcp") == "mcp" and not o.get("url"):
            out[name] = plugin.mcp
    return out

def safe(name):  # LightRAG workspace: a-z A-Z 0-9 _
    return re.sub(r"[^A-Za-z0-9_]", "_", name)

LIGHTRAG_ENV = {
    "HOST": "0.0.0.0", "PORT": "9621", "WORKING_DIR": "/app/data/rag_storage", "INPUT_DIR": "/app/data/inputs",
    "LIGHTRAG_API_KEY": "${LIGHTRAG_API_KEY}",
    "LLM_BINDING": "${LLM_PROVIDER:-ollama}", "LLM_BINDING_HOST": "${LLM_BASE_URL:-http://ollama:11434}",
    "LLM_BINDING_API_KEY": "${LLM_API_KEY:-ollama}", "LLM_MODEL": "${LLM_MODEL}", "OLLAMA_LLM_NUM_CTX": "32768",
    "EMBEDDING_BINDING": "${EMBED_PROVIDER:-ollama}", "EMBEDDING_BINDING_HOST": "${EMBED_BASE_URL:-http://ollama:11434}",
    "EMBEDDING_BINDING_API_KEY": "${EMBED_API_KEY:-ollama}",
    "EMBEDDING_MODEL": "${EMBED_MODEL}", "EMBEDDING_DIM": "${EMBED_DIM}",
    # concurrency is capped per instance so N scopes ingesting cannot starve queries on a shared model endpoint
    "LLM_TIMEOUT": "600", "MAX_ASYNC": "${LIGHTRAG_MAX_ASYNC:-2}", "MAX_PARALLEL_INSERT": "${LIGHTRAG_MAX_PARALLEL_INSERT:-1}",
    "MAX_GLEANING": "${LIGHTRAG_MAX_GLEANING:-0}",    # 0 = one extraction pass per chunk (halves LLM calls); 1 = LightRAG default
    # hidden reasoning off for every LightRAG call (Ollama binding only): measured 60 s / 6,560 output tokens per chunk with
    # qwen3.6:35b-mlx thinking vs 7 s / 675 tokens without, same entities. num_predict caps runaway outputs.
    "OLLAMA_LLM_THINK": "${LIGHTRAG_LLM_THINK:-false}", "OLLAMA_LLM_NUM_PREDICT": "${LIGHTRAG_MAX_OUTPUT_TOKENS:-4096}",
    "WHITELIST_PATHS": "/health",
    "LIGHTRAG_KV_STORAGE": "PGKVStorage", "LIGHTRAG_DOC_STATUS_STORAGE": "PGDocStatusStorage",
    "LIGHTRAG_VECTOR_STORAGE": "PGVectorStorage", "LIGHTRAG_GRAPH_STORAGE": "PGTableGraphStorage",
    "POSTGRES_HOST": "postgres", "POSTGRES_PORT": "5432", "POSTGRES_USER": "${POSTGRES_USER}",
    "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD}", "POSTGRES_DATABASE": "lightrag", "POSTGRES_VECTOR_INDEX_TYPE": "HNSW",
}

PROJECT_LABEL = {"context-stack.io/project": "agent-context-stack"}


def compose():
    services, volumes = {}, {}
    for name, sc in scopes.items():
        ws = safe(name)
        labels = {**PROJECT_LABEL, "context-stack.io/scope": name}
        services[f"lightrag-{name}"] = {
            "image": "ghcr.io/hkuds/lightrag:v1.5.7", "restart": "unless-stopped",
            "depends_on": {"postgres": {"condition": "service_healthy"}},
            "environment": {**LIGHTRAG_ENV, "WORKSPACE": f"scope_{ws}"},
            "volumes": [f"lightrag_{ws}:/app/data"], "labels": labels,
        }
        volumes[f"lightrag_{ws}"] = {"labels": labels}
        services[f"falkordb-{name}"] = {
            "image": "docker.io/falkordb/falkordb:v4.20.4", "restart": "unless-stopped",
            "volumes": [f"falkordb_{ws}:/var/lib/falkordb/data"], "labels": labels,
            "healthcheck": {"test": ["CMD", "redis-cli", "PING"], "interval": "10s", "timeout": "5s", "retries": 10},
        }
        volumes[f"falkordb_{ws}"] = {"labels": labels}
        services[f"codegraph-{name}"] = {
            "build": "../mcp/codegraph-mcp", "restart": "unless-stopped",
            "depends_on": {f"falkordb-{name}": {"condition": "service_healthy"}},
            "environment": {"DEFAULT_DATABASE": "falkordb-remote", "FALKORDB_HOST": f"falkordb-{name}", "FALKORDB_PORT": "6379"},
            "volumes": [f"repos_{ws}:/workspace:ro", f"cgc_{ws}:/home/cgc/.codegraphcontext"], "labels": labels,
        }
        volumes[f"repos_{ws}"] = {"labels": labels}; volumes[f"cgc_{ws}"] = {"labels": labels}
        services[f"indexer-{name}"] = {
            "build": "../mcp/codegraph-mcp", "profiles": ["jobs"],
            "depends_on": {f"falkordb-{name}": {"condition": "service_healthy"}},
            "environment": {"DEFAULT_DATABASE": "falkordb-remote", "FALKORDB_HOST": f"falkordb-{name}", "FALKORDB_PORT": "6379",
                            "GIT_DOC_REPOS": ",".join((sc.get("code") or {}).get("repos", [])), "GITHUB_TOKEN": "${GITHUB_TOKEN}"},
            "volumes": [f"repos_{ws}:/workspace", f"cgc_{ws}:/home/cgc/.codegraphcontext",
                        "../index/index-repo.sh:/usr/local/bin/index-repo.sh:ro"],
            "entrypoint": ["/bin/bash", "/usr/local/bin/index-repo.sh", "all"], "labels": labels,
        }
    ups = mcp_upstreams()
    for name, m in ups.items():
        services[f"mcp-{name}"] = {"image": m.image, "restart": "unless-stopped", "command": list(m.args),
                                   "environment": dict(m.env), "labels": {**PROJECT_LABEL, "context-stack.io/plugin": name}}   # no ports
    if ups:
        services["gateway"] = {"depends_on": {f"mcp-{n}": {"condition": "service_started"} for n in ups}}
    out = {"services": services, "volumes": volumes}
    print("# GENERATED by scripts/gen-scopes.py from config/scopes.yaml and plugins/ — do not edit by hand.\n"
          "# Lives in docker/ next to compose.yaml; paths are relative to that directory. Use: make up")
    print(yaml.safe_dump(out, sort_keys=False, width=120))

# Names the engines expect, derived from the short names in config/stack.env. Kubernetes has no ${VAR} interpolation
# for Secrets, so the generator materialises them into the stack-env Secret.
DERIVED_ENV = {
    "LLM_BINDING": "{LLM_PROVIDER}", "LLM_BINDING_HOST": "{LLM_BASE_URL}", "LLM_BINDING_API_KEY": "{LLM_API_KEY}",
    "EMBEDDING_BINDING": "{EMBED_PROVIDER}", "EMBEDDING_BINDING_HOST": "{EMBED_BASE_URL}",
    "EMBEDDING_BINDING_API_KEY": "{EMBED_API_KEY}", "EMBEDDING_MODEL": "{EMBED_MODEL}", "EMBEDDING_DIM": "{EMBED_DIM}",
    "HINDSIGHT_API_DATABASE_URL": "postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@postgres:5432/hindsight",
    "HINDSIGHT_API_LLM_PROVIDER": "{LLM_PROVIDER}", "HINDSIGHT_API_LLM_BASE_URL": "{LLM_BASE_URL}",
    "HINDSIGHT_API_LLM_MODEL": "{LLM_MODEL}", "HINDSIGHT_API_LLM_API_KEY": "{LLM_API_KEY}",
    "HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL": "{HINDSIGHT_EMBED_BASE_URL}",
    "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY": "{EMBED_API_KEY}",
    "HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL": "{EMBED_MODEL}", "HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS": "{EMBED_DIM}",
    "HINDSIGHT_API_RERANKER_PROVIDER": "{HINDSIGHT_RERANKER}",
    "HINDSIGHT_API_TENANT_API_KEY": "{HINDSIGHT_API_KEY}", "HINDSIGHT_CP_DATAPLANE_API_KEY": "{HINDSIGHT_API_KEY}",
    "DATABASE_URL": "postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@postgres:5432/sourcebot",
    "CONFLUENCE_USERNAME": "{CONFLUENCE_USER}", "CONFLUENCE_API_TOKEN": "{CONFLUENCE_TOKEN}",   # mcp-atlassian names
    "AUTH_URL": "{SOURCEBOT_AUTH_URL}", "AUTH_SECRET": "{SOURCEBOT_AUTH_SECRET}",
    # LightRAG knobs: compose reads the ${LIGHTRAG_*:-default} forms in LIGHTRAG_ENV; Kubernetes gets them from here
    "MAX_ASYNC": "{LIGHTRAG_MAX_ASYNC}", "MAX_PARALLEL_INSERT": "{LIGHTRAG_MAX_PARALLEL_INSERT}",
    "MAX_GLEANING": "{LIGHTRAG_MAX_GLEANING}", "OLLAMA_LLM_THINK": "{LIGHTRAG_LLM_THINK}",
    "OLLAMA_LLM_NUM_PREDICT": "{LIGHTRAG_MAX_OUTPUT_TOKENS}",
}

def load_env() -> dict | None:
    """config/stack.env plus the derived engine-specific names (DERIVED_ENV). None when the file does not exist."""
    src = pathlib.Path(__file__).parent.parent / "config/stack.env"
    if not src.exists():
        return None
    base = {}
    for line in src.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); base[k.strip()] = v.strip()
    for k, v in {"LLM_PROVIDER": "ollama", "LLM_BASE_URL": "http://ollama:11434", "LLM_API_KEY": "ollama",
                 "EMBED_PROVIDER": "ollama", "EMBED_BASE_URL": "http://ollama:11434", "EMBED_API_KEY": "ollama",
                 "HINDSIGHT_EMBED_BASE_URL": "http://ollama:11434/v1", "HINDSIGHT_RERANKER": "local",
                 "LIGHTRAG_MAX_ASYNC": "2", "LIGHTRAG_MAX_PARALLEL_INSERT": "1", "LIGHTRAG_MAX_GLEANING": "0",
                 "LIGHTRAG_LLM_THINK": "false", "LIGHTRAG_MAX_OUTPUT_TOKENS": "4096"}.items():
        base.setdefault(k, v)
    class _D(dict):
        def __missing__(self, k): return ""
    return {**base, **{k: v.format_map(_D(base)) for k, v in DERIVED_ENV.items()}}


def k8s():
    """k8s/generated/: per-scope manifests + kustomization with Secret (from config/stack.env) and ConfigMaps (from config/)."""
    root = pathlib.Path(__file__).parent.parent
    out = root / "k8s/generated"
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*"):
        shutil.rmtree(f) if f.is_dir() else f.unlink()
    env = load_env()
    if env is None:
        sys.exit("error: config/stack.env not found (copy config/stack.env.example first)")
    (out / "stack-env.env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    (out / "stack-env.env").chmod(0o600)
    for name, src in {"scopes.yaml": "config/scopes.yaml", "config.json": "config/sourcebot/config.json",
                      "init.sql": "config/postgres/init.sql"}.items():
        (out / name).write_text((root / src).read_text())
    plug = out / "plugins"; plug.mkdir()
    plugin_files = sorted((root / "plugins").glob("*.py"))
    for f in plugin_files:
        (plug / f.name).write_text(f.read_text())
    plugin_list = ", ".join(f"plugins/{f.name}" for f in plugin_files)
    lightrag_env = {k: (v if not v.startswith("${") else None) for k, v in LIGHTRAG_ENV.items()}
    resources = []
    for name, sc in scopes.items():
        ws = safe(name)
        env_lines = "\n".join(f"            - {{ name: {k}, value: \"{v}\" }}" for k, v in lightrag_env.items() if v is not None)
        repos = ",".join((sc.get("code") or {}).get("repos", []))
        (out / f"scope-{name}.yaml").write_text(f"""# GENERATED by scripts/gen-scopes.py k8s for scope "{name}" -- do not edit.
apiVersion: v1
kind: Service
metadata: {{ name: lightrag-{name}, labels: {{ scope: {name} }} }}
spec:
  selector: {{ app: lightrag-{name} }}
  ports: [{{ port: 9621, targetPort: 9621 }}]
---
apiVersion: apps/v1
kind: Deployment
metadata: {{ name: lightrag-{name}, labels: {{ scope: {name} }} }}
spec:
  replicas: 1
  strategy: {{ type: Recreate }}
  selector: {{ matchLabels: {{ app: lightrag-{name} }} }}
  template:
    metadata: {{ labels: {{ app: lightrag-{name}, scope: {name} }} }}
    spec:
      containers:
        - name: lightrag
          image: ghcr.io/hkuds/lightrag:v1.5.7
          envFrom: [{{ secretRef: {{ name: stack-env }} }}]   # LLM_BINDING*, EMBEDDING_*, POSTGRES_*, LIGHTRAG_API_KEY
          env:
{env_lines}
            - {{ name: WORKSPACE, value: scope_{ws} }}
          ports: [{{ containerPort: 9621 }}]
          volumeMounts: [{{ name: data, mountPath: /app/data }}]
          readinessProbe:
            httpGet: {{ path: /health, port: 9621 }}
            initialDelaySeconds: 20
            periodSeconds: 15
      volumes:
        - name: data
          persistentVolumeClaim: {{ claimName: lightrag-{name}-data }}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {{ name: lightrag-{name}-data }}
spec: {{ accessModes: [ReadWriteOnce], resources: {{ requests: {{ storage: 5Gi }} }} }}
---
apiVersion: v1
kind: Service
metadata: {{ name: falkordb-{name}, labels: {{ scope: {name} }} }}
spec:
  selector: {{ app: falkordb-{name} }}
  ports: [{{ port: 6379, targetPort: 6379 }}]
---
apiVersion: apps/v1
kind: StatefulSet
metadata: {{ name: falkordb-{name}, labels: {{ scope: {name} }} }}
spec:
  serviceName: falkordb-{name}
  replicas: 1
  selector: {{ matchLabels: {{ app: falkordb-{name} }} }}
  template:
    metadata: {{ labels: {{ app: falkordb-{name}, scope: {name} }} }}
    spec:
      containers:
        - name: falkordb
          image: docker.io/falkordb/falkordb:v4.20.4
          ports: [{{ containerPort: 6379 }}]
          volumeMounts: [{{ name: data, mountPath: /var/lib/falkordb/data }}]
          readinessProbe:
            exec: {{ command: ["redis-cli", "PING"] }}
            periodSeconds: 10
  volumeClaimTemplates:
    - metadata: {{ name: data }}
      spec: {{ accessModes: [ReadWriteOnce], resources: {{ requests: {{ storage: 5Gi }} }} }}
---
apiVersion: v1
kind: Service
metadata: {{ name: codegraph-{name}, labels: {{ scope: {name} }} }}
spec:
  selector: {{ app: codegraph-{name} }}
  ports: [{{ port: 8045, targetPort: 8045 }}]
---
apiVersion: apps/v1
kind: Deployment
metadata: {{ name: codegraph-{name}, labels: {{ scope: {name} }} }}
spec:
  replicas: 1
  strategy: {{ type: Recreate }}
  selector: {{ matchLabels: {{ app: codegraph-{name} }} }}
  template:
    metadata: {{ labels: {{ app: codegraph-{name}, scope: {name} }} }}
    spec:
      containers:
        - name: codegraph
          image: agent-context-stack-codegraph-public:latest
          imagePullPolicy: IfNotPresent
          env:
            - {{ name: DEFAULT_DATABASE, value: falkordb-remote }}
            - {{ name: FALKORDB_HOST, value: falkordb-{name} }}
            - {{ name: FALKORDB_PORT, value: "6379" }}
          ports: [{{ containerPort: 8045 }}]
          volumeMounts:
            - {{ name: repos, mountPath: /workspace, readOnly: true }}
            - {{ name: cgc, mountPath: /home/cgc/.codegraphcontext }}
      volumes:
        - name: repos
          persistentVolumeClaim: {{ claimName: repos-{name} }}
        - name: cgc
          persistentVolumeClaim: {{ claimName: cgc-{name} }}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {{ name: repos-{name} }}
spec: {{ accessModes: [ReadWriteOnce], resources: {{ requests: {{ storage: 10Gi }} }} }}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {{ name: cgc-{name} }}
spec: {{ accessModes: [ReadWriteOnce], resources: {{ requests: {{ storage: 1Gi }} }} }}
---
# Nightly re-index. One-off run:  kubectl -n context-stack create job --from=cronjob/indexer-{name} indexer-{name}-now
apiVersion: batch/v1
kind: CronJob
metadata: {{ name: indexer-{name}, labels: {{ scope: {name} }} }}
spec:
  schedule: "0 3 * * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      backoffLimit: 1
      template:
        metadata: {{ labels: {{ app: indexer-{name}, scope: {name} }} }}
        spec:
          restartPolicy: Never
          containers:
            - name: indexer
              image: agent-context-stack-codegraph-public:latest
              imagePullPolicy: IfNotPresent
              command: ["/bin/bash", "/usr/local/bin/index-repo.sh", "all"]
              envFrom: [{{ secretRef: {{ name: stack-env }} }}]   # GITHUB_TOKEN for private repos
              env:
                - {{ name: DEFAULT_DATABASE, value: falkordb-remote }}
                - {{ name: FALKORDB_HOST, value: falkordb-{name} }}
                - {{ name: FALKORDB_PORT, value: "6379" }}
                - {{ name: GIT_DOC_REPOS, value: "{repos}" }}
              volumeMounts:
                - {{ name: repos, mountPath: /workspace }}
                - {{ name: cgc, mountPath: /home/cgc/.codegraphcontext }}
                - {{ name: script, mountPath: /usr/local/bin/index-repo.sh, subPath: index-repo.sh, readOnly: true }}
          volumes:
            - name: repos
              persistentVolumeClaim: {{ claimName: repos-{name} }}
            - name: cgc
              persistentVolumeClaim: {{ claimName: cgc-{name} }}
            - name: script
              configMap: {{ name: index-script, defaultMode: 0o755 }}
""")
        resources.append(f"scope-{name}.yaml")
    (out / "index-repo.sh").write_text((root / "index/index-repo.sh").read_text())
    for name, m in mcp_upstreams().items():
        env_lines = "\n".join(f"            - {{ name: {k}, value: \"{v}\" }}" for k, v in m.env.items())
        (out / f"mcp-{name}.yaml").write_text(f"""# GENERATED from plugins/{name}.py (McpUpstream) -- the live fallback's upstream MCP server. ClusterIP only.
apiVersion: v1
kind: Service
metadata: {{ name: mcp-{name} }}
spec:
  selector: {{ app: mcp-{name} }}
  ports: [{{ port: {m.port}, targetPort: {m.port} }}]
---
apiVersion: apps/v1
kind: Deployment
metadata: {{ name: mcp-{name} }}
spec:
  replicas: 1
  selector: {{ matchLabels: {{ app: mcp-{name} }} }}
  template:
    metadata: {{ labels: {{ app: mcp-{name} }} }}
    spec:
      containers:
        - name: mcp
          image: {m.image}
          args: {yaml.safe_dump(list(m.args), default_flow_style=True).strip()}
          envFrom: [{{ secretRef: {{ name: stack-env }} }}]
          env:
{env_lines}
          ports: [{{ containerPort: {m.port} }}]
""")
        resources.append(f"mcp-{name}.yaml")
    (out / "kustomization.yaml").write_text("""# GENERATED by scripts/gen-scopes.py k8s -- do not edit, do not commit.
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
""" + "".join(f"  - {r}\n" for r in resources) + """secretGenerator:
  - name: stack-env
    envs: [stack-env.env]
configMapGenerator:
  - name: scopes-config
    files: [scopes.yaml]
  - name: sourcebot-config
    files: [config.json]
  - name: postgres-init
    files: [init.sql]
  - name: index-script
    files: [index-repo.sh]
  - name: plugins
    files: [PLUGIN_FILES]
generatorOptions:
  disableNameSuffixHash: true
""".replace("PLUGIN_FILES", plugin_list))
    print(f"wrote k8s/generated for scopes: {', '.join(scopes)}")


{"compose": compose, "k8s": k8s}[sys.argv[1]]()

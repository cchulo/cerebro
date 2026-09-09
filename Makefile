# Compose project: docker/compose.yaml (shared services) + docker/compose.scopes.yaml (generated per-scope services).
# Host Ollama (Apple silicon etc.): make up EXTRA="-f docker/compose.host-ollama.yaml"   NVIDIA: EXTRA="-f docker/compose.gpu.yaml"
export COMPOSE_ENV_FILES = $(CURDIR)/config/stack.env
COMPOSE ?= docker compose -f docker/compose.yaml -f docker/compose.scopes.yaml $(EXTRA)
PYTHON  ?= python3
SCOPES  ?= $(shell $(PYTHON) -c "import yaml;print(' '.join(yaml.safe_load(open('config/scopes.yaml'))['scopes']))")

.PHONY: gen up down nuke build models index sync smoke logs ps status test-env k8s-apply k8s-status source-check
gen:     ; $(PYTHON) scripts/gen-scopes.py compose > docker/compose.scopes.yaml && $(PYTHON) scripts/gen-scopes.py k8s
build:   ; $(COMPOSE) build
# lifecycle: scripts/up.sh / scripts/down.sh take the same flags (see their headers)
up:      ; PYTHON=$(PYTHON) scripts/up.sh $(ARGS)
down:    ; scripts/down.sh $(ARGS)
# complete teardown: containers + volumes on compose AND kubernetes (images kept; add --images/--pulled to drop them)
nuke:    ; scripts/down.sh --volumes --all-targets
ps:      ; $(COMPOSE) ps
# is it working / is it progressing (services, documents processed per scope, code graph, last sync): make status ARGS=--watch
status:  ; scripts/status.sh $(ARGS)
models:  ; ./scripts/pull-models.sh
index:   ; for s in $(SCOPES); do $(COMPOSE) --profile jobs run --rm indexer-$$s; done
sync:    ; curl -fsS -X POST localhost:8080/sync/all -H "X-Ingest-Secret: $$(grep ^INGEST_WEBHOOK_SECRET config/stack.env | cut -d= -f2)"
smoke:   ; $(PYTHON) scripts/smoke-test.py $(ARGS)
# list what an adapter would ingest for a scope, without LightRAG:  make source-check SCOPE=public SOURCE=confluence
source-check: ; $(COMPOSE) run --rm --no-deps ingest python -m ingest.check $(SCOPE) $(SOURCE) $(ARGS)
logs:    ; $(COMPOSE) logs -f --tail=100
# mock Confluence/Backstage + fixture docs (see docker/compose.test.yaml); then: make sync && make smoke ARGS=--live
test-env: ; PYTHON=$(PYTHON) scripts/up.sh --test $(ARGS)
# Kubernetes (k3s / OrbStack): same images, same service names. See k8s/README.md
k8s-apply:  ; kubectl apply -k k8s
k8s-status: ; scripts/status.sh --k8s $(ARGS)

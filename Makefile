# Compose project: shared services (compose.yaml) + generated per-scope services (compose.scopes.yaml).
# Host Ollama (Apple silicon etc.): make up EXTRA="-f compose.host-ollama.yaml"   NVIDIA: EXTRA="-f compose.gpu.yaml"
export COMPOSE_ENV_FILES = config/stack.env
COMPOSE ?= docker compose -f compose.yaml -f compose.scopes.yaml $(EXTRA)
PYTHON  ?= python3
SCOPES  ?= $(shell $(PYTHON) -c "import yaml;print(' '.join(yaml.safe_load(open('config/scopes.yaml'))['scopes']))")

.PHONY: gen up down build models index sync smoke logs ps test-env k8s-apply k8s-status
gen:     ; $(PYTHON) scripts/gen-scopes.py compose > compose.scopes.yaml && $(PYTHON) scripts/gen-scopes.py k8s
build:   ; $(COMPOSE) build
up:      ; $(COMPOSE) up -d
down:    ; $(COMPOSE) down
ps:      ; $(COMPOSE) ps
models:  ; ./scripts/pull-models.sh
index:   ; for s in $(SCOPES); do $(COMPOSE) --profile jobs run --rm indexer-$$s; done
sync:    ; curl -fsS -X POST localhost:8080/sync/all -H "X-Ingest-Secret: $$(grep ^INGEST_WEBHOOK_SECRET config/stack.env | cut -d= -f2)"
smoke:   ; $(PYTHON) scripts/smoke-test.py $(ARGS)
logs:    ; $(COMPOSE) logs -f --tail=100
# mock Confluence/Backstage + fixture docs (see compose.test.yaml); then: make sync && make smoke ARGS=--live
test-env: ; $(COMPOSE) -f compose.test.yaml up -d --build
# Kubernetes (k3s / OrbStack): same images, same service names. See k8s/README.md
k8s-apply:  ; kubectl apply -k k8s
k8s-status: ; kubectl -n context-stack get pods,pvc

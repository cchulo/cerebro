# Compose project: shared services (compose.yaml) + generated per-scope services (compose.scopes.yaml).
# Host Ollama (Apple silicon etc.): make up EXTRA="-f compose.host-ollama.yaml"   NVIDIA: EXTRA="-f compose.gpu.yaml"
COMPOSE ?= docker compose -f compose.yaml -f compose.scopes.yaml $(EXTRA)
SCOPES  ?= $(shell python3 -c "import yaml;print(' '.join(yaml.safe_load(open('config/scopes.yaml'))['scopes']))")

.PHONY: gen up down build models index sync smoke logs ps test-env
gen:     ; python3 scripts/gen-scopes.py compose > compose.scopes.yaml && python3 scripts/gen-scopes.py quadlet
build:   ; $(COMPOSE) build
up:      ; $(COMPOSE) up -d
down:    ; $(COMPOSE) down
ps:      ; $(COMPOSE) ps
models:  ; ./scripts/pull-models.sh
index:   ; for s in $(SCOPES); do $(COMPOSE) --profile jobs run --rm indexer-$$s; done
sync:    ; curl -fsS -X POST localhost:8080/sync/all -H "X-Ingest-Secret: $$(grep ^INGEST_WEBHOOK_SECRET .env | cut -d= -f2)"
smoke:   ; python3 scripts/smoke-test.py $(ARGS)
logs:    ; $(COMPOSE) logs -f --tail=100
# mock Confluence/Backstage + fixture docs (see compose.test.yaml); then: make sync && make smoke ARGS=--live
test-env: ; $(COMPOSE) -f compose.test.yaml up -d --build

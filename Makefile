.PHONY: up down models index sync logs
up:      ; docker compose up -d
down:    ; docker compose down
models:  ; ./scripts/pull-models.sh
index:   ; docker compose --profile jobs run --rm indexer
sync:    ; curl -fsS -X POST localhost:8080/sync/all -H "X-Ingest-Secret: $$(grep INGEST_WEBHOOK_SECRET .env | cut -d= -f2)"
logs:    ; docker compose logs -f --tail=100

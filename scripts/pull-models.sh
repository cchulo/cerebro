#!/usr/bin/env sh
# Pull the models Hindsight and LightRAG expect. Run after `compose up -d ollama`.
set -e
: "${LLM_MODEL:=gpt-oss:20b}"
: "${EMBED_MODEL:=bge-m3}"
docker compose exec ollama ollama pull "$LLM_MODEL"
docker compose exec ollama ollama pull "$EMBED_MODEL"

# cerebro ingest: the sync engine + webhook/schedule service. Build from the repository root:
#   docker build -f images/ingest/Dockerfile -t cerebro-ingest .
# Mount cerebro.yaml at /app/cerebro.yaml (or set CEREBRO_CONFIG) and secrets as env; plugins ship in the image.
FROM python:3.12-slim
# git: the `git` docs plugin shallow-clones the scope's repositories
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY cerebro ./cerebro
RUN pip install --no-cache-dir '.[ingest]'
COPY plugins ./plugins
ENV CEREBRO_CONFIG=/app/cerebro.yaml \
    CEREBRO_PLUGINS_DIR=/app/plugins \
    CEREBRO_STATE_DIR=/state \
    PYTHONUNBUFFERED=1
VOLUME ["/state"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"
CMD ["cerebro", "ingest", "serve", "--host", "0.0.0.0", "--port", "8080"]

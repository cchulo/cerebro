# The MCP gateway. Build context is the repository root:
#     docker build -f images/gateway/Dockerfile -t cerebro/gateway:dev .
# Runtime: mount cerebro.yaml at /config/cerebro.yaml (or set CEREBRO_CONFIG) and pass secrets as env vars
# (secrets.source env). Inside a container identity.mode none binds to 127.0.0.1 and is unreachable from outside
# by design; set identity.allow_remote (with CEREBRO_TOKEN) or use static/builtin/external to publish the port.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CEREBRO_CONFIG=/config/cerebro.yaml
COPY pyproject.toml README.md ./
COPY cerebro ./cerebro
RUN pip install --no-cache-dir '.[gateway]'
COPY plugins ./plugins
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request;u=os.environ.get('CEREBRO_HEALTH_URL','http://127.0.0.1:8090/health');urllib.request.urlopen(u,timeout=4)" || exit 1
CMD ["cerebro", "gateway", "serve"]

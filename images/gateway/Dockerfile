# The MCP gateway. Build context is the repository root:
#     docker build -f images/gateway/Dockerfile -t cerebro/gateway:dev .
# Runtime: mount cerebro.yaml at /config/cerebro.yaml (or set CEREBRO_CONFIG) and pass secrets as env vars
# (secrets.source env). The provisioner sets CEREBRO_GATEWAY_BIND=0.0.0.0 (a published port cannot reach a loopback
# listener) and, in identity.mode none, CEREBRO_TRUSTED_NETWORK=1 (the peer is the bridge, not loopback) while
# publishing the port on the host's 127.0.0.1 only. Run by hand without those, mode none binds 127.0.0.1 and is
# unreachable from outside by design.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CEREBRO_CONFIG=/config/cerebro.yaml
COPY pyproject.toml README.md ./
COPY cerebro ./cerebro
RUN pip install --no-cache-dir '.[gateway]'
COPY plugins ./plugins
EXPOSE 8090
# /health on the port the gateway serves: $CEREBRO_GATEWAY_PORT (the provisioner sets it), else gateway.port from
# the mounted cerebro.yaml; CEREBRO_HEALTH_URL overrides the whole URL.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request;p=os.environ.get('CEREBRO_GATEWAY_PORT') or str(__import__('cerebro.core',fromlist=['load_config']).load_config().gateway.port);urllib.request.urlopen(os.environ.get('CEREBRO_HEALTH_URL','http://127.0.0.1:%s/health'%p),timeout=4)" || exit 1
CMD ["cerebro", "gateway", "serve"]

# CodeGraphContext's MCP server is stdio-only; supergateway exposes it as streamable HTTP for remote clients.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl git ca-certificates \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir codegraphcontext \
 && npm install -g supergateway
EXPOSE 8045
# verify `cgc mcp start` is the MCP entrypoint for the installed cgc version (`cgc --help`)
CMD ["supergateway", "--stdio", "cgc mcp start", "--outputTransport", "streamableHttp", \
     "--port", "8045", "--streamableHttpPath", "/mcp"]

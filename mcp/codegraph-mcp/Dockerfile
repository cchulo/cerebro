# CodeGraphContext (MIT) MCP server for one scope, bridged from stdio to streamable HTTP by supergateway.
# Same image runs the per-scope indexer job (index/index-repo.sh).
# Verified with codegraphcontext 0.6.13: `cgc mcp start` = stdio MCP server; DEFAULT_DATABASE=falkordb-remote +
# FALKORDB_HOST/PORT = external FalkorDB (plain `falkordb` would mean the embedded FalkorDB Lite).
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl git ca-certificates \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir codegraphcontext==0.6.13 \
 && npm install -g supergateway@3.4.3
# Pre-create the two mount points owned by cgc so named volumes inherit that ownership on first use.
RUN useradd -m -u 1000 cgc && mkdir -p /workspace /home/cgc/.codegraphcontext \
 && chown -R cgc:cgc /workspace /home/cgc/.codegraphcontext
USER cgc
# cgc keeps its config/state in $HOME/.codegraphcontext (mounted per scope). CGC_ALLOWED_ROOTS limits what the
# MCP tools may index/watch even if such a tool were ever reached.
ENV HOME=/home/cgc \
    DEFAULT_DATABASE=falkordb-remote \
    CGC_ALLOWED_ROOTS=/workspace \
    CGC_ACTIVE_PROGRESS_BAR=false
WORKDIR /workspace
EXPOSE 8045
CMD ["supergateway", "--stdio", "cgc mcp start", "--outputTransport", "streamableHttp", \
     "--port", "8045", "--streamableHttpPath", "/mcp"]

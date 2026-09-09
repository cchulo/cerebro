FROM python:3.12-slim
WORKDIR /app
COPY mcp/gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sdk /opt/sdk
RUN pip install --no-cache-dir /opt/sdk        # stack_plugins: the plugin framework
COPY mcp/gateway/gateway ./gateway
EXPOSE 8090
CMD ["python", "-m", "gateway.server"]

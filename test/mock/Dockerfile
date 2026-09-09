# Mock Confluence + Backstage APIs serving fixture data, for testing the ingest without real systems.
FROM python:3.12-slim
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn>=0.32" "pyyaml==6.*" "mcp>=1.30,<2"
WORKDIR /app
COPY app.py mcp_app.py .
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

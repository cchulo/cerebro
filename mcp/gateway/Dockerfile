FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gateway ./gateway
EXPOSE 8090
CMD ["python", "-m", "gateway.server"]

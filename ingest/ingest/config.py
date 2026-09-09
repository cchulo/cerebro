"""Settings of the ingest service itself. Adapter settings live in the adapters (see sources/base.py)."""
import os

def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)

LIGHTRAG_API_KEY = env("LIGHTRAG_API_KEY")
WEBHOOK_SECRET = env("INGEST_WEBHOOK_SECRET")
SCHEDULE_CRON = env("INGEST_SCHEDULE_CRON", "0 2 * * *")
STATE_DIR = env("STATE_DIR", "/state")

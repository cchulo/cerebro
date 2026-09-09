import os

def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)

def env_list(name: str) -> list[str]:
    return [x.strip() for x in env(name).split(",") if x.strip()]

LIGHTRAG_URL = env("LIGHTRAG_URL", "http://lightrag:9621")
LIGHTRAG_API_KEY = env("LIGHTRAG_API_KEY")

CONFLUENCE_URL = env("CONFLUENCE_URL").rstrip("/")
CONFLUENCE_USER = env("CONFLUENCE_USER")
CONFLUENCE_TOKEN = env("CONFLUENCE_TOKEN")

BACKSTAGE_URL = env("BACKSTAGE_URL").rstrip("/")
BACKSTAGE_TOKEN = env("BACKSTAGE_TOKEN")

GIT_DOC_GLOBS = env_list("GIT_DOC_GLOBS") or ["README.md", "docs/**/*.md", "adr/**/*.md"]
GITHUB_TOKEN = env("GITHUB_TOKEN")

WEBHOOK_SECRET = env("INGEST_WEBHOOK_SECRET")
SCHEDULE_CRON = env("INGEST_SCHEDULE_CRON", "0 2 * * *")
STATE_DIR = env("STATE_DIR", "/state")

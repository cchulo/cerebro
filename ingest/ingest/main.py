"""Ingest service: scheduled + webhook-triggered sync of document sources into per-scope LightRAG instances.

  GET  /health
  GET  /sources                              adapters in use and whether they are configured
  POST /sync/all                             (header X-Ingest-Secret)
  POST /sync/{source}                        one adapter across all scopes
  POST /webhook/{source}   body = adapter filter, e.g. confluence {"space": "ENG"}, git {"repo": "https://.../x.git"}
"""
import logging, threading
from fastapi import FastAPI, Header, HTTPException, BackgroundTasks, Body
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from . import scopes, sources, lightrag, sync as syncer
from .config import WEBHOOK_SECRET, SCHEDULE_CRON

log = logging.getLogger("ingest")
logging.basicConfig(level=logging.INFO)
app = FastAPI(title="agent-context-stack ingest")
_lock = threading.Lock()   # one sync at a time


def _guard(secret: str | None):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(401, "bad secret")


def _run(name: str, source: str | None, filter: dict | None = None):
    if not _lock.acquire(blocking=False):
        log.warning("sync %s skipped: another sync running", name); return
    try:
        log.info("sync %s start", name)
        log.info("sync %s done: %s", name, syncer.sync(source, filter))
    except Exception:
        log.exception("sync %s failed", name)
    finally:
        _lock.release()


def _known(source: str):
    if source not in scopes.source_names():
        raise HTTPException(404, f"no scope uses source '{source}'; in use: {scopes.source_names()}")


@app.get("/health")
def health():
    return {"ok": True, "lightrag_scopes": lightrag.health()}


@app.get("/sources")
def list_sources():
    return {n: {"configured": sources.load(n, scopes.SOURCES.get(n)).configured(),
                "scopes": [s for s in scopes.SCOPES if n in scopes.docs_config(s)]} for n in scopes.source_names()}


@app.post("/sync/{source}")
def trigger_sync(source: str, bg: BackgroundTasks, x_ingest_secret: str | None = Header(default=None)):
    _guard(x_ingest_secret)
    if source == "all":
        bg.add_task(_run, "all", None); return {"queued": "all"}
    _known(source)
    bg.add_task(_run, source, source); return {"queued": source}


@app.post("/webhook/{source}")
def webhook(source: str, bg: BackgroundTasks, body: dict = Body(default={}),
            x_ingest_secret: str | None = Header(default=None)):
    _guard(x_ingest_secret)
    _known(source)
    bg.add_task(_run, f"{source}:{body}", source, body or None)
    return {"queued": source, "filter": body}


_sched = BackgroundScheduler()
_sched.add_job(lambda: _run("scheduled", None), CronTrigger.from_crontab(SCHEDULE_CRON), id="scheduled")
_sched.start()

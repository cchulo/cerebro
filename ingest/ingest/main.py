"""Ingest service: scheduled + webhook-triggered sync of Confluence, Backstage and Git docs into LightRAG.

  GET  /health
  POST /sync/{confluence|backstage|git|all}     (header X-Ingest-Secret)
  POST /webhook/confluence   body: {"space": "ENG"}           (optional)
  POST /webhook/backstage
  POST /webhook/git          body: {"repo": "https://github.com/org/x.git"}
"""
import logging, threading
from fastapi import FastAPI, Header, HTTPException, BackgroundTasks
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from . import confluence, backstage, gitdocs, lightrag
from .config import WEBHOOK_SECRET, SCHEDULE_CRON

log = logging.getLogger("ingest")
logging.basicConfig(level=logging.INFO)
app = FastAPI(title="agent-context-stack ingest")
_lock = threading.Lock()   # one sync at a time

def _guard(secret: str | None):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(401, "bad secret")

def _run(name: str, fn, *args):
    if not _lock.acquire(blocking=False):
        log.warning("sync %s skipped: another sync running", name); return
    try:
        log.info("sync %s start", name)
        log.info("sync %s done: %s", name, fn(*args))
    except Exception:
        log.exception("sync %s failed", name)
    finally:
        _lock.release()

def sync_all():
    _run("confluence", confluence.sync)
    _run("backstage", backstage.sync)
    _run("git", gitdocs.sync)

class GitHook(BaseModel):
    repo: str
class ConfluenceHook(BaseModel):
    space: str | None = None

@app.get("/health")
def health():
    return {"ok": True, "lightrag_scopes": lightrag.health()}

@app.post("/sync/{source}")
def sync(source: str, bg: BackgroundTasks, x_ingest_secret: str | None = Header(default=None)):
    _guard(x_ingest_secret)
    fns = {"confluence": lambda: _run("confluence", confluence.sync),
           "backstage": lambda: _run("backstage", backstage.sync),
           "git": lambda: _run("git", gitdocs.sync),
           "all": sync_all}
    if source not in fns:
        raise HTTPException(404, "unknown source")
    bg.add_task(fns[source]); return {"queued": source}

@app.post("/webhook/git")
def hook_git(body: GitHook, bg: BackgroundTasks, x_ingest_secret: str | None = Header(default=None)):
    _guard(x_ingest_secret)
    bg.add_task(_run, "git", gitdocs.sync, [body.repo]); return {"queued": body.repo}

@app.post("/webhook/confluence")
def hook_confluence(body: ConfluenceHook, bg: BackgroundTasks, x_ingest_secret: str | None = Header(default=None)):
    _guard(x_ingest_secret)
    bg.add_task(_run, "confluence", confluence.sync, [body.space] if body.space else None)
    return {"queued": body.space or "all spaces"}

@app.post("/webhook/backstage")
def hook_backstage(bg: BackgroundTasks, x_ingest_secret: str | None = Header(default=None)):
    _guard(x_ingest_secret)
    bg.add_task(_run, "backstage", backstage.sync); return {"queued": "backstage"}

_sched = BackgroundScheduler()
_sched.add_job(sync_all, CronTrigger.from_crontab(SCHEDULE_CRON), id="nightly")
_sched.start()

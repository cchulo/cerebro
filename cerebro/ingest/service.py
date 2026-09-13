"""Ingest service: scheduled + webhook-triggered sync of document sources into the per-scope indexes.

  GET  /health                               service up + every scope's index health
  GET  /sources                              plugins in use, whether they are configured, which scopes use them
  POST /sync/all                             (header X-Ingest-Secret, secret INGEST_WEBHOOK_SECRET)
  POST /sync/{source}                        one plugin across all scopes
  POST /webhook/{source}   body = plugin filter, e.g. confluence {"space": "ENG"}, git {"repo": "https://.../x.git"}

Schedule: engines.docs.options.schedule, else $INGEST_SCHEDULE_CRON, else "0 2 * * *". One sync runs at a time;
a trigger that arrives while one runs is skipped with a log line (the scheduled run picks everything up).
"""
from __future__ import annotations
import asyncio, logging, os, threading
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException, BackgroundTasks, Body
from .runtime import Ingest
from .sync import sync as run_sync

log = logging.getLogger("cerebro.ingest.service")
DEFAULT_SCHEDULE = "0 2 * * *"


def schedule_for(ingest: Ingest) -> str:
    docs = ingest.config.engines.docs
    return str((docs.options.get("schedule") if docs else None) or os.environ.get("INGEST_SCHEDULE_CRON", DEFAULT_SCHEDULE))


def create_app(ingest: Ingest, *, schedule: str | None = None, scheduler: bool = True) -> FastAPI:
    secret = ingest.secret("INGEST_WEBHOOK_SECRET")
    cron = schedule or schedule_for(ingest)
    lock = threading.Lock()                       # one sync at a time
    runs: list[dict] = []                         # last outcomes, newest last (for tests and /health)

    def run(label: str, source: str | None, filter: dict | None = None) -> None:
        if not lock.acquire(blocking=False):
            log.warning("sync %s skipped: another sync running", label)
            runs.append({"run": label, "skipped": "another sync running"})
            return
        try:
            log.info("sync %s start", label)
            report = run_sync(ingest, source, filter)
            log.info("sync %s done: %s", label, report)
            runs.append({"run": label, "report": report})
        except Exception as e:
            log.exception("sync %s failed", label)
            runs.append({"run": label, "error": f"{type(e).__name__}: {e}"})
        finally:
            del runs[:-20]
            lock.release()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sched = None
        if scheduler:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
            sched = BackgroundScheduler()
            sched.add_job(lambda: run("scheduled", None), CronTrigger.from_crontab(cron), id="scheduled")
            sched.start()
            log.info("scheduled sync: %s", cron)
        try:
            yield
        finally:
            if sched is not None:
                sched.shutdown(wait=False)

    app = FastAPI(title="cerebro ingest", lifespan=lifespan)
    app.state.ingest, app.state.runs, app.state.schedule, app.state.run = ingest, runs, cron, run

    def guard(header: str | None) -> None:
        if secret and header != secret:
            raise HTTPException(401, "bad secret")

    def known(source: str) -> None:
        names = ingest.source_names()
        if source not in names:
            raise HTTPException(404, f"no scope uses source '{source}'; in use: {names}")

    @app.get("/health")
    async def health():
        scopes = ingest.scopes()
        results = await asyncio.gather(*(ingest.index.health(s) for s in scopes), return_exceptions=True)
        index = {}
        for s, h in zip(scopes, results):
            index[s] = {"ok": False, "detail": f"{type(h).__name__}: {h}".rstrip(": ")} if isinstance(h, Exception) \
                else h.model_dump(exclude_defaults=True) | {"ok": h.ok}
        return {"ok": True, "engine": ingest.index.name, "schedule": cron, "syncing": lock.locked(),
                "index": index, "last_run": runs[-1] if runs else None}

    @app.get("/sources")
    def list_sources():
        out = {}
        for n in ingest.source_names():
            try:
                configured = ingest.sources.load(n).configured()
            except KeyError as e:
                configured, note = False, str(e)
            else:
                note = None
            out[n] = {"configured": configured, "scopes": ingest.scopes_using(n)} | ({"error": note} if note else {})
        return out

    @app.post("/sync/{source}")
    def trigger_sync(source: str, bg: BackgroundTasks, x_ingest_secret: str | None = Header(default=None)):
        guard(x_ingest_secret)
        if source == "all":
            bg.add_task(run, "all", None)
            return {"queued": "all"}
        known(source)
        bg.add_task(run, source, source)
        return {"queued": source}

    @app.post("/webhook/{source}")
    def webhook(source: str, bg: BackgroundTasks, body: dict = Body(default={}),
                x_ingest_secret: str | None = Header(default=None)):
        guard(x_ingest_secret)
        known(source)
        bg.add_task(run, f"{source}:{body}", source, body or None)
        return {"queued": source, "filter": body}

    return app

"""The FastAPI ingest service over httpx's ASGITransport (background tasks complete before the response returns)."""
import httpx, pytest
from fastapi.testclient import TestClient
from cerebro.ingest.service import create_app, schedule_for

H = {"X-Ingest-Secret": "s3"}


@pytest.fixture
def app(ingest):
    return create_app(ingest, scheduler=False)


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ingest") as c:
        yield c


async def test_health_reports_every_scope_index(client, index):
    index.down.add("infra")
    r = await client.get("/health")
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True and body["engine"] == "fake" and body["schedule"] == "0 2 * * *"
    assert body["index"]["public"]["ok"] is True and body["index"]["infra"] == {"ok": False, "detail": "unit stopped"}
    assert body["last_run"] is None and body["syncing"] is False


async def test_sources_lists_plugins_and_scopes(client):
    r = await client.get("/sources")
    assert r.json() == {"files": {"configured": True, "scopes": ["public", "infra"]}}


async def test_sync_requires_the_secret(client, index):
    assert (await client.post("/sync/all")).status_code == 401
    assert (await client.post("/sync/all", headers={"X-Ingest-Secret": "wrong"})).status_code == 401
    assert index.batches == []


async def test_sync_all_runs_in_the_background(client, index, app):
    r = await client.post("/sync/all", headers=H)
    assert r.status_code == 200 and r.json() == {"queued": "all"}
    assert [b.scope for b in index.batches] == ["public", "infra"]
    last = app.state.runs[-1]
    assert last["run"] == "all" and last["report"]["public/files"] == {"changed": 4, "removed": 0}
    assert (await client.get("/health")).json()["last_run"]["run"] == "all"


async def test_sync_one_source_and_unknown_source(client, index):
    assert (await client.post("/sync/nope", headers=H)).status_code == 404
    r = await client.post("/sync/files", headers=H)
    assert r.json() == {"queued": "files"} and len(index.batches) == 2


async def test_webhook_passes_the_filter(client, index, docs_dir, app):
    await client.post("/sync/all", headers=H)
    (docs_dir / "public" / "deploy.md").unlink()
    r = await client.post("/webhook/files", headers=H, json={"path": "public"})
    assert r.status_code == 200 and r.json() == {"queued": "files", "filter": {"path": "public"}}
    # the files plugin's covers() returns False for any filter: a filtered run never deletes
    assert app.state.runs[-1]["report"]["public/files"] == {"changed": 0, "removed": 0}
    r = await client.post("/webhook/files", headers=H)                  # empty body = unfiltered
    assert app.state.runs[-1]["report"]["public/files"] == {"changed": 0, "removed": 1}
    assert index.all_deletes() >= {"files:public:public/deploy.md"}
    assert (await client.post("/webhook/nope", headers=H, json={})).status_code == 404


async def test_failed_sync_is_recorded_not_raised(client, index, app):
    index.fail = RuntimeError("boom")
    r = await client.post("/sync/all", headers=H)
    assert r.status_code == 200 and app.state.runs[-1]["error"] == "RuntimeError: boom"


def test_no_secret_configured_means_open_endpoints(ingest, index):
    ingest.ctx.secrets.values.pop("INGEST_WEBHOOK_SECRET")
    app = create_app(ingest, scheduler=False)
    with TestClient(app) as c:
        assert c.post("/sync/all").status_code == 200 and len(index.batches) == 2


def test_schedule_from_options_env_or_default(ingest, monkeypatch):
    monkeypatch.delenv("INGEST_SCHEDULE_CRON", raising=False)
    assert schedule_for(ingest) == "0 2 * * *"
    monkeypatch.setenv("INGEST_SCHEDULE_CRON", "*/30 * * * *")
    assert schedule_for(ingest) == "*/30 * * * *"
    ingest.config.engines.docs.options["schedule"] = "0 4 * * 1"
    assert schedule_for(ingest) == "0 4 * * 1"
    assert create_app(ingest, schedule="0 5 * * *", scheduler=False).state.schedule == "0 5 * * *"


def test_scheduler_starts_and_stops_with_the_app(ingest):
    app = create_app(ingest, schedule="0 3 * * *")
    with TestClient(app) as c:
        assert c.get("/health").json()["schedule"] == "0 3 * * *"

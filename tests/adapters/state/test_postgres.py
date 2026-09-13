"""Postgres SyncState. The contract run needs a live database: set CEREBRO_TEST_PG_DSN (e.g.
"host=localhost user=cerebro password=... dbname=cerebro"); the table is created and emptied by the test."""
import os
import pytest
from cerebro.adapters.state.postgres import Adapter, TABLE
from tests.contracts.state import SyncStateContract

DSN = os.environ.get("CEREBRO_TEST_PG_DSN")


@pytest.mark.skipif(not DSN, reason="CEREBRO_TEST_PG_DSN not set")
class TestPostgresContract(SyncStateContract):
    @pytest.fixture
    def adapter(self, ctx):
        a = Adapter({"dsn": DSN}, ctx)
        a._run(lambda conn: conn.execute(f"DROP TABLE IF EXISTS {TABLE}"))
        a._ready = False
        yield a
        a.close()


def test_dsn_from_options_env_and_secret(ctx, monkeypatch):
    assert Adapter({"dsn": "postgresql://u:p@h/db"}, ctx).dsn() == "postgresql://u:p@h/db"
    for k in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_DATABASE", "POSTGRES_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    assert Adapter({}, ctx).dsn() == "host=postgres port=5432 user=cerebro dbname=cerebro"
    ctx.secrets.values["POSTGRES_PASSWORD"] = "s3cret"
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    a = Adapter({"database": "other"}, ctx)
    assert a.dsn() == "host=db.internal port=5432 user=cerebro dbname=other password=s3cret" and a.configured()


def test_not_configured_without_password(ctx, monkeypatch):
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    assert Adapter({}, ctx).configured() is False

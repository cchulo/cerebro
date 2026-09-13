"""SyncState in the shared Postgres unit, so several ingest replicas (and a restarted one) agree on what was seen.

Table `cerebro_sync_state(key text primary key, version text not null, updated_at timestamptz)` in database
`cerebro`, created on first use. Connection: options.dsn, else built from POSTGRES_HOST / POSTGRES_PORT /
POSTGRES_USER / POSTGRES_DATABASE (env, defaults postgres / 5432 / cerebro / cerebro) with the password from the
secret POSTGRES_PASSWORD. Needs psycopg 3 (the `ingest` extra).
"""
from __future__ import annotations
import os, threading
from cerebro.core.contracts.state import SyncState

TABLE = "cerebro_sync_state"
DEFAULTS = {"host": "postgres", "port": "5432", "user": "cerebro", "database": "cerebro"}
_DDL = f"""CREATE TABLE IF NOT EXISTS {TABLE} (
    key text PRIMARY KEY,
    version text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
)"""


class Adapter(SyncState):
    name = "postgres"

    def __init__(self, options=None, ctx=None):
        super().__init__(options, ctx)
        self._lock = threading.Lock()
        self._conn = None
        self._ready = False

    # ---------------------------------------------------------------------------------------------- connection
    def dsn(self) -> str:
        """options.dsn wins; otherwise host/port/user/database from options, then env, then defaults."""
        if self.option("dsn"):
            return str(self.option("dsn"))
        env = os.environ
        host = self.option("host") or env.get("POSTGRES_HOST", DEFAULTS["host"])
        port = str(self.option("port") or env.get("POSTGRES_PORT", DEFAULTS["port"]))
        user = self.option("user") or env.get("POSTGRES_USER", DEFAULTS["user"])
        database = self.option("database") or env.get("POSTGRES_DATABASE", DEFAULTS["database"])
        password = (self.ctx.secret("POSTGRES_PASSWORD") if self.ctx else None) or env.get("POSTGRES_PASSWORD")
        parts = [f"host={host}", f"port={port}", f"user={user}", f"dbname={database}"]
        if password:
            parts.append(f"password={password}")
        return " ".join(parts)

    def configured(self) -> bool:
        try:
            import psycopg  # noqa: F401
        except ImportError:
            return False
        return bool(self.option("dsn") or self.ctx is None or self.ctx.secret("POSTGRES_PASSWORD") or os.environ.get("POSTGRES_PASSWORD"))

    def _connection(self):
        import psycopg
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn(), autocommit=False)
            self._ready = False
        if not self._ready:
            with self._conn.cursor() as cur:
                cur.execute(_DDL)
            self._conn.commit()
            self._ready = True
        return self._conn

    def _run(self, fn):
        """Run fn(conn) under the lock; drop the connection on any error so the next call reconnects."""
        with self._lock:
            conn = self._connection()
            try:
                out = fn(conn)
                conn.commit()
                return out
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    self._conn = None
                raise

    def close(self) -> None:
        with self._lock:
            if self._conn is not None and not self._conn.closed:
                self._conn.close()
            self._conn = None

    # ---------------------------------------------------------------------------------------------- contract
    def get(self, key: str) -> str | None:
        def q(conn):
            with conn.cursor() as cur:
                cur.execute(f"SELECT version FROM {TABLE} WHERE key = %s", (key,))
                row = cur.fetchone()
            return row[0] if row else None
        return self._run(q)

    def keys_with_prefix(self, prefix: str) -> list[str]:
        def q(conn):
            with conn.cursor() as cur:
                cur.execute(f"SELECT key FROM {TABLE} WHERE left(key, %s) = %s ORDER BY key", (len(prefix), prefix))
                return [r[0] for r in cur.fetchall()]
        return self._run(q)

    def commit(self, set_items: dict[str, str], delete_keys: set[str] | list[str] = ()) -> None:
        def q(conn):
            with conn.cursor() as cur:
                if delete_keys:
                    cur.execute(f"DELETE FROM {TABLE} WHERE key = ANY(%s)", (list(delete_keys),))
                if set_items:
                    cur.executemany(
                        f"INSERT INTO {TABLE} (key, version, updated_at) VALUES (%s, %s, now()) "
                        f"ON CONFLICT (key) DO UPDATE SET version = EXCLUDED.version, updated_at = now()",
                        list(set_items.items()))
        self._run(q)

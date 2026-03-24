"""Database init and connection — SQLite (default) or PostgreSQL."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, List, Optional

from carbon.config import get_settings

# Schema works for both SQLite and PostgreSQL
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS runs (
        id              TEXT PRIMARY KEY,
        scenario_name   TEXT NOT NULL,
        provider        TEXT NOT NULL,
        parameters      TEXT NOT NULL,
        status          TEXT NOT NULL,
        started_at      TIMESTAMP,
        completed_at    TIMESTAMP,
        summary         TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS actions (
        id              TEXT PRIMARY KEY,
        run_id          TEXT NOT NULL,
        phase           TEXT NOT NULL,
        action_type     TEXT NOT NULL,
        parameters      TEXT NOT NULL,
        dependencies   TEXT,
        status          TEXT NOT NULL,
        result          TEXT,
        error           TEXT,
        executed_at     TIMESTAMP,
        retry_count     INTEGER DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS entity_map (
        run_id          TEXT NOT NULL,
        local_id        TEXT NOT NULL,
        entity_type     TEXT NOT NULL,
        remote_id       TEXT,
        provider        TEXT NOT NULL,
        state           TEXT,
        metadata        TEXT,
        created_at      TIMESTAMP,
        PRIMARY KEY (run_id, local_id)
    )""",
    """CREATE TABLE IF NOT EXISTS findings (
        id              TEXT PRIMARY KEY,
        run_id          TEXT NOT NULL,
        check_name      TEXT NOT NULL,
        severity        TEXT NOT NULL,
        passed          SMALLINT NOT NULL,
        message         TEXT,
        details         TEXT,
        created_at      TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS webhook_deliveries (
        id              TEXT PRIMARY KEY,
        run_id          TEXT NOT NULL,
        target_url      TEXT NOT NULL,
        event_type      TEXT NOT NULL,
        entity_type     TEXT,
        local_id        TEXT,
        remote_id       TEXT,
        status_code     INTEGER,
        ok              SMALLINT NOT NULL,
        error           TEXT,
        duration_ms     INTEGER,
        response_body   TEXT,
        sent_at         TIMESTAMP NOT NULL,
        payload         TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_actions_run ON actions(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_findings_severity ON findings(severity)",
    "CREATE INDEX IF NOT EXISTS idx_webhook_run ON webhook_deliveries(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_webhook_event ON webhook_deliveries(run_id, event_type)",
]


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _sqlite_path(url: str) -> str:
    """Extract file path from sqlite:///path or sqlite:path."""
    path = re.sub(r"^sqlite:(?:///)?", "", url)
    return str(Path(path).expanduser())


def _sqlite_to_pg_placeholders(query: str) -> str:
    """Convert ? placeholders to $1, $2, ... for asyncpg."""
    n = 0
    def repl(_: Any) -> str:
        nonlocal n
        n += 1
        return f"${n}"
    return re.sub(r"\?", repl, query)


# ---------------------------------------------------------------------------
# SQLite connection wrapper (aiosqlite)
# ---------------------------------------------------------------------------

class _SQLiteConnection:
    """Wrapper around aiosqlite with the same interface as _PostgresConnection."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def execute(self, query: str, *params: Any) -> None:
        args = params[0] if len(params) == 1 and isinstance(params[0], (list, tuple)) else params
        await self._conn.execute(query, args)
        await self._conn.commit()

    async def fetch(self, query: str, *params: Any) -> List[dict]:
        args = params[0] if len(params) == 1 and isinstance(params[0], (list, tuple)) else params
        cursor = await self._conn.execute(query, args)
        rows = await cursor.fetchall()
        columns = [d[0] for d in cursor.description] if cursor.description else []
        return [dict(zip(columns, row)) for row in rows]

    async def fetchrow(self, query: str, *params: Any) -> Optional[dict]:
        args = params[0] if len(params) == 1 and isinstance(params[0], (list, tuple)) else params
        cursor = await self._conn.execute(query, args)
        row = await cursor.fetchone()
        if row is None:
            return None
        columns = [d[0] for d in cursor.description] if cursor.description else []
        return dict(zip(columns, row))

    async def close(self) -> None:
        await self._conn.close()

    async def __aenter__(self) -> "_SQLiteConnection":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


# ---------------------------------------------------------------------------
# PostgreSQL connection wrapper (asyncpg)
# ---------------------------------------------------------------------------

class _PostgresConnection:
    """Wrapper so repo can use ? placeholders and dict-like rows."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def execute(self, query: str, *params: Any) -> None:
        q = _sqlite_to_pg_placeholders(query)
        args = list(params[0]) if len(params) == 1 and isinstance(params[0], (list, tuple)) else list(params)
        await self._conn.execute(q, *args)

    async def fetch(self, query: str, *params: Any) -> List[dict]:
        q = _sqlite_to_pg_placeholders(query)
        args = list(params[0]) if len(params) == 1 and isinstance(params[0], (list, tuple)) else list(params)
        rows = await self._conn.fetch(q, *args)
        return [dict(r) for r in rows]

    async def fetchrow(self, query: str, *params: Any) -> Optional[dict]:
        q = _sqlite_to_pg_placeholders(query)
        args = list(params[0]) if len(params) == 1 and isinstance(params[0], (list, tuple)) else list(params)
        row = await self._conn.fetchrow(q, *args)
        return dict(row) if row else None

    async def close(self) -> None:
        await self._conn.close()

    async def __aenter__(self) -> "_PostgresConnection":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


# ---------------------------------------------------------------------------
# Init and connection factory
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """Create tables if they do not exist."""
    settings = get_settings()
    url = settings.database_url

    if _is_sqlite(url):
        import aiosqlite

        db_path = _sqlite_path(url)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(db_path)
        try:
            for stmt in SCHEMA:
                await conn.execute(stmt)
            await conn.commit()
        finally:
            await conn.close()
    else:
        import asyncpg

        conn = await asyncpg.connect(url)
        try:
            for stmt in SCHEMA:
                await conn.execute(stmt)
        finally:
            await conn.close()


async def get_connection():
    """Return a connection wrapper. Tables are created on first use."""
    await init_db()
    settings = get_settings()
    url = settings.database_url

    if _is_sqlite(url):
        import aiosqlite

        db_path = _sqlite_path(url)
        conn = await aiosqlite.connect(db_path)
        return _SQLiteConnection(conn)
    else:
        import asyncpg

        conn = await asyncpg.connect(url)
        return _PostgresConnection(conn)

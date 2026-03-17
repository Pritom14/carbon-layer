"""Repository helpers for runs, actions, entity_map, findings."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, List, Optional

from carbon.storage.db import get_connection, init_db


def _json_dump(obj: dict | list | None) -> Optional[str]:
    if obj is None:
        return None
    return json.dumps(obj)


def _json_load(s: str | None) -> Any:
    if s is None:
        return None
    return json.loads(s)


async def ensure_db() -> None:
    await init_db()


async def create_run(
    scenario_name: str,
    provider: str,
    parameters: dict,
) -> str:
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    conn = await get_connection()
    try:
        await conn.execute(
            """
            INSERT INTO runs (id, scenario_name, provider, parameters, status, started_at, completed_at, summary)
            VALUES (?, ?, ?, ?, 'pending', NULL, NULL, NULL)
            """,
            (run_id, scenario_name, provider, _json_dump(parameters)),
        )
    finally:
        await conn.close()
    return run_id


async def update_run_status(
    run_id: str,
    status: str,
    completed_at: Optional[datetime] = None,
    summary: Optional[dict] = None,
    conn: Optional[Any] = None,
) -> None:
    own_conn = conn is None
    if conn is None:
        conn = await get_connection()
    try:
        if completed_at is not None and summary is not None:
            await conn.execute(
                """UPDATE runs SET status = ?, completed_at = ?, summary = ? WHERE id = ?""",
                (status, completed_at, _json_dump(summary), run_id),
            )
        elif completed_at is not None:
            await conn.execute(
                """UPDATE runs SET status = ?, completed_at = ? WHERE id = ?""",
                (status, completed_at, run_id),
            )
        else:
            await conn.execute(
                """UPDATE runs SET status = ? WHERE id = ?""",
                (status, run_id),
            )
    finally:
        if own_conn:
            await conn.close()


async def set_run_started(run_id: str, conn: Optional[Any] = None) -> None:
    own_conn = conn is None
    if conn is None:
        conn = await get_connection()
    try:
        await conn.execute(
            """UPDATE runs SET status = 'running', started_at = ? WHERE id = ?""",
            (datetime.utcnow(), run_id),
        )
    finally:
        if own_conn:
            await conn.close()


async def insert_action(
    run_id: str,
    phase: str,
    action_type: str,
    parameters: dict,
    dependencies: Optional[List[str]],
    status: str = "pending",
    result: Optional[dict] = None,
    error: Optional[str] = None,
    retry_count: int = 0,
    conn: Optional[Any] = None,
) -> str:
    action_id = f"act_{uuid.uuid4().hex[:12]}"
    own_conn = conn is None
    if conn is None:
        conn = await get_connection()
    try:
        await conn.execute(
            """
            INSERT INTO actions (id, run_id, phase, action_type, parameters, dependencies, status, result, error, executed_at, retry_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id,
                run_id,
                phase,
                action_type,
                _json_dump(parameters),
                _json_dump(dependencies) if dependencies else None,
                status,
                _json_dump(result),
                error,
                datetime.utcnow() if result or error else None,
                retry_count,
            ),
        )
    finally:
        if own_conn:
            await conn.close()
    return action_id


async def update_action_result(action_id: str, result: Optional[dict] = None, error: Optional[str] = None, conn: Optional[Any] = None) -> None:
    own_conn = conn is None
    if conn is None:
        conn = await get_connection()
    try:
        await conn.execute(
            """UPDATE actions SET status = ?, result = ?, error = ?, executed_at = ? WHERE id = ?""",
            ("failed" if error else "completed", _json_dump(result), error, datetime.utcnow(), action_id),
        )
    finally:
        if own_conn:
            await conn.close()


async def insert_entity_mapping(
    run_id: str,
    local_id: str,
    entity_type: str,
    remote_id: Optional[str],
    provider: str,
    state: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    created = datetime.utcnow()
    meta_json = _json_dump(metadata)
    conn = await get_connection()
    try:
        await conn.execute(
            """
            INSERT INTO entity_map (run_id, local_id, entity_type, remote_id, provider, state, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id, local_id) DO UPDATE SET
                entity_type = EXCLUDED.entity_type,
                remote_id = EXCLUDED.remote_id,
                state = EXCLUDED.state,
                metadata = EXCLUDED.metadata,
                created_at = EXCLUDED.created_at
            """,
            (run_id, local_id, entity_type, remote_id or "", provider, state, meta_json, created),
        )
    finally:
        await conn.close()


def _entity_map_row(local_id: str, entity_type: str, remote_id: str, state: Optional[str], metadata: Optional[dict]) -> dict:
    return {"local_id": local_id, "entity_type": entity_type, "remote_id": remote_id or "", "state": state, "metadata": metadata}


async def insert_entity_mappings_bulk(conn: Any, run_id: str, provider: str, rows: List[dict]) -> None:
    """Insert many entity_map rows in one or few round-trips. Each row: local_id, entity_type, remote_id, state, metadata."""
    if not rows:
        return
    created = datetime.utcnow()
    BATCH = 300
    for i in range(0, len(rows), BATCH):
        chunk = rows[i : i + BATCH]
        values: List[Any] = []
        placeholders: List[str] = []
        for r in chunk:
            placeholders.append("(?, ?, ?, ?, ?, ?, ?, ?)")
            values.extend((
                run_id,
                r["local_id"],
                r["entity_type"],
                r.get("remote_id") or "",
                provider,
                r.get("state"),
                _json_dump(r.get("metadata")),
                created,
            ))
        query = """
            INSERT INTO entity_map (run_id, local_id, entity_type, remote_id, provider, state, metadata, created_at)
            VALUES """ + ", ".join(placeholders) + """
            ON CONFLICT (run_id, local_id) DO UPDATE SET
                entity_type = EXCLUDED.entity_type,
                remote_id = EXCLUDED.remote_id,
                state = EXCLUDED.state,
                metadata = EXCLUDED.metadata,
                created_at = EXCLUDED.created_at
        """
        await conn.execute(query, tuple(values))


async def insert_finding(
    run_id: str,
    check_name: str,
    severity: str,
    passed: bool,
    message: Optional[str] = None,
    details: Optional[dict] = None,
) -> str:
    finding_id = f"fin_{uuid.uuid4().hex[:12]}"
    conn = await get_connection()
    try:
        await conn.execute(
            """
            INSERT INTO findings (id, run_id, check_name, severity, passed, message, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (finding_id, run_id, check_name, severity, 1 if passed else 0, message, _json_dump(details), datetime.utcnow()),
        )
    finally:
        await conn.close()
    return finding_id


async def get_run(run_id: str) -> Optional[dict]:
    conn = await get_connection()
    try:
        row = await conn.fetchrow("SELECT * FROM runs WHERE id = ?", (run_id,))
    finally:
        await conn.close()
    if row is None:
        return None
    return {
        "id": row["id"],
        "scenario_name": row["scenario_name"],
        "provider": row["provider"],
        "parameters": _json_load(row["parameters"]),
        "status": row["status"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "summary": _json_load(row["summary"]),
    }


async def get_actions_for_run(run_id: str) -> List[dict]:
    conn = await get_connection()
    try:
        rows = await conn.fetch("SELECT * FROM actions WHERE run_id = ? ORDER BY executed_at", (run_id,))
    finally:
        await conn.close()
    return [
        {
            "id": r["id"],
            "phase": r["phase"],
            "action_type": r["action_type"],
            "parameters": _json_load(r["parameters"]),
            "status": r["status"],
            "result": _json_load(r["result"]),
            "error": r["error"],
        }
        for r in rows
    ]


async def get_entity_map(run_id: str, conn: Optional[Any] = None) -> dict:
    """Return dict keyed by local_id: {entity_type, remote_id, ...}."""
    own_conn = conn is None
    if conn is None:
        conn = await get_connection()
    try:
        rows = await conn.fetch(
            "SELECT local_id, entity_type, remote_id, state, metadata FROM entity_map WHERE run_id = ?",
            (run_id,),
        )
    finally:
        if own_conn:
            await conn.close()
    def _state(r: dict) -> Optional[str]:
        v = r.get("state")
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None

    return {
        r["local_id"]: {
            "entity_type": r["entity_type"],
            "remote_id": r["remote_id"],
            "state": _state(r),
            "metadata": _json_load(r["metadata"]),
        }
        for r in rows
    }


async def get_findings(run_id: str) -> List[dict]:
    conn = await get_connection()
    try:
        rows = await conn.fetch(
            """
            SELECT * FROM findings WHERE run_id = ?
            ORDER BY
                CASE severity
                    WHEN 'critical' THEN 1 WHEN 'high' THEN 2 WHEN 'medium' THEN 3 WHEN 'low' THEN 4
                    ELSE 5 END,
                created_at
            """,
            (run_id,),
        )
    finally:
        await conn.close()
    return [
        {
            "id": r["id"],
            "check_name": r["check_name"],
            "severity": r["severity"],
            "passed": bool(r["passed"]),
            "message": r["message"],
            "details": _json_load(r["details"]),
        }
        for r in rows
    ]


async def insert_webhook_deliveries_bulk(
    conn: Any,
    run_id: str,
    target_url: str,
    deliveries: List[dict],
) -> None:
    """Insert many webhook delivery rows in one or few round-trips."""
    if not deliveries:
        return
    BATCH = 300
    for i in range(0, len(deliveries), BATCH):
        chunk = deliveries[i : i + BATCH]
        values: List[Any] = []
        placeholders: List[str] = []
        for d in chunk:
            placeholders.append("(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
            values.extend(
                (
                    d["id"],
                    run_id,
                    target_url,
                    d["event_type"],
                    d.get("entity_type"),
                    d.get("local_id"),
                    d.get("remote_id"),
                    d.get("status_code"),
                    1 if d.get("ok") else 0,
                    d.get("error"),
                    d.get("duration_ms"),
                    d.get("response_body"),
                    d["sent_at"],
                )
            )
        query = (
            """
            INSERT INTO webhook_deliveries
              (id, run_id, target_url, event_type, entity_type, local_id, remote_id, status_code, ok, error, duration_ms, response_body, sent_at)
            VALUES """
            + ", ".join(placeholders)
        )
        await conn.execute(query, tuple(values))


async def get_webhook_deliveries(run_id: str) -> List[dict]:
    conn = await get_connection()
    try:
        rows = await conn.fetch(
            """
            SELECT * FROM webhook_deliveries WHERE run_id = ?
            ORDER BY
                CASE
                    WHEN ok = 0 AND status_code IS NULL THEN 1
                    WHEN ok = 0 THEN 2
                    ELSE 3
                END,
                sent_at
            """,
            (run_id,),
        )
    finally:
        await conn.close()
    return [
        {
            "id": r["id"],
            "run_id": r["run_id"],
            "target_url": r["target_url"],
            "event_type": r["event_type"],
            "entity_type": r["entity_type"],
            "local_id": r["local_id"],
            "remote_id": r["remote_id"],
            "status_code": r["status_code"],
            "ok": bool(r["ok"]),
            "error": r["error"],
            "duration_ms": r["duration_ms"],
            "response_body": r["response_body"],
            "sent_at": r["sent_at"],
        }
        for r in rows
    ]

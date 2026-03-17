"""Async webhook sender with delivery recording."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import datetime
from typing import Any

import httpx

from carbon.storage.db import get_connection
from carbon.storage.repo import get_entity_map, insert_webhook_deliveries_bulk
from carbon.webhook.payloads import build_events_from_entity_map


async def _post_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    target_url: str,
    event: dict,
    timeout_s: float,
    secret: str,
) -> dict:
    async with sem:
        sent_at = datetime.utcnow()
        t0 = asyncio.get_event_loop().time()
        event_id = f"wh_{uuid.uuid4().hex[:12]}"
        status_code = None
        ok = False
        error = None
        response_body = None
        try:
            body = json.dumps(event["payload"], separators=(",", ":"), sort_keys=True).encode("utf-8")
            signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            headers = {
                "Content-Type": "application/json",
                "X-Razorpay-Event-Id": event_id,
                "X-Razorpay-Signature": signature,
            }
            resp = await client.post(target_url, content=body, headers=headers, timeout=timeout_s)
            status_code = resp.status_code
            ok = 200 <= resp.status_code < 300
            # Store small response body for debugging; truncate to avoid huge DB rows.
            if resp.text:
                response_body = resp.text[:500]
        except Exception as e:
            error = str(e)
        duration_ms = int((asyncio.get_event_loop().time() - t0) * 1000)
        return {
            "id": event_id,
            "event_type": event["event_type"],
            "entity_type": event.get("entity_type"),
            "local_id": event.get("local_id"),
            "remote_id": event.get("remote_id"),
            "status_code": status_code,
            "ok": ok,
            "error": error,
            "duration_ms": duration_ms,
            "response_body": response_body,
            "sent_at": sent_at,
        }


async def send_webhooks(
    run_id: str,
    *,
    target_url: str,
    secret: str = "carbon",
    concurrency: int = 25,
    timeout_s: float = 5.0,
    account_id: str = "acc_carbon",
) -> list[dict]:
    """
    Build Razorpay-format webhooks from entity_map, POST them to target_url,
    and record delivery attempts in webhook_deliveries.
    """
    conn = await get_connection()
    try:
        entity_map = await get_entity_map(run_id, conn=conn)
        events = build_events_from_entity_map(entity_map, account_id=account_id)
        if not events:
            return []
        sem = asyncio.Semaphore(max(1, int(concurrency)))
        async with httpx.AsyncClient() as client:
            deliveries = await asyncio.gather(
                *[_post_one(client, sem, target_url, e, timeout_s, secret) for e in events]
            )
        await insert_webhook_deliveries_bulk(conn, run_id, target_url, deliveries)
        return deliveries
    finally:
        await conn.close()


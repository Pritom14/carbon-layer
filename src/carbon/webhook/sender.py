"""Async webhook sender with provider-aware signing and delivery recording."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime
from typing import Any

import httpx

from carbon.storage.db import get_connection
from carbon.storage.repo import get_entity_map, insert_webhook_deliveries_bulk
from carbon.webhook.payloads import build_events_from_entity_map


def _sign_razorpay(body: bytes, secret: str) -> dict[str, str]:
    """Razorpay signing: HMAC-SHA256 of the raw JSON body."""
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": signature,
    }


def _sign_stripe(body: bytes, secret: str) -> dict[str, str]:
    """Stripe signing: t=timestamp,v1=HMAC-SHA256(secret, timestamp.body)."""
    timestamp = str(int(time.time()))
    signed_payload = f"{timestamp}.".encode("utf-8") + body
    signature = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "Stripe-Signature": f"t={timestamp},v1={signature}",
    }


def _sign_cashfree(body: bytes, secret: str) -> dict[str, str]:
    """Cashfree signing: Base64(HMAC-SHA256(timestamp + body, secret))."""
    import base64

    timestamp = str(int(time.time() * 1000))  # milliseconds
    signed_payload = timestamp.encode("utf-8") + body
    signature = base64.b64encode(
        hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).digest()
    ).decode("utf-8")
    return {
        "Content-Type": "application/json",
        "x-webhook-signature": signature,
        "x-webhook-timestamp": timestamp,
        "x-webhook-version": "2025-01-01",
    }


_SIGNERS = {
    "razorpay": _sign_razorpay,
    "stripe": _sign_stripe,
    "cashfree": _sign_cashfree,
}


async def _post_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    target_url: str,
    event: dict,
    timeout_s: float,
    secret: str,
    provider: str,
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
            signer = _SIGNERS.get(provider, _sign_razorpay)
            headers = signer(body, secret)
            resp = await client.post(target_url, content=body, headers=headers, timeout=timeout_s)
            status_code = resp.status_code
            ok = 200 <= resp.status_code < 300
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
    provider: str = "razorpay",
) -> list[dict]:
    """
    Build provider-format webhooks from entity_map, POST them to target_url,
    and record delivery attempts in webhook_deliveries.
    """
    conn = await get_connection()
    try:
        entity_map = await get_entity_map(run_id, conn=conn)
        events = build_events_from_entity_map(entity_map, account_id=account_id, provider=provider)
        if not events:
            return []
        sem = asyncio.Semaphore(max(1, int(concurrency)))
        async with httpx.AsyncClient() as client:
            deliveries = await asyncio.gather(
                *[_post_one(client, sem, target_url, e, timeout_s, secret, provider) for e in events]
            )
        await insert_webhook_deliveries_bulk(conn, run_id, target_url, deliveries)
        return deliveries
    finally:
        await conn.close()

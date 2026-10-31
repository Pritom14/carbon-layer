"""Client-side "verify payment" calls, the second path that often credits a payment.

After Razorpay Checkout succeeds, the browser receives razorpay_order_id,
razorpay_payment_id and razorpay_signature (HMAC-SHA256 of "order_id|payment_id"
keyed with the API key secret) and posts them to the app's verify endpoint. Many apps
credit the user there *and* again in the payment.captured webhook. Deduping on the
webhook event id can't catch that: the two requests are different events for the
same payment. Firing both lets --verify-cmd check one effect per payment.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac

import httpx


def razorpay_checkout_signature(order_id: str, payment_id: str, key_secret: str) -> str:
    return hmac.new(key_secret.encode("utf-8"), f"{order_id}|{payment_id}".encode("utf-8"), hashlib.sha256).hexdigest()


def captured_payments(events: list[dict], event_type: str = "payment.captured") -> list[dict]:
    """Unique (order_id, payment_id) pairs from built Razorpay-format events."""
    seen: dict[str, dict] = {}
    for e in events:
        if e.get("event_type") != event_type:
            continue
        entity = e.get("payload", {}).get("payload", {}).get("payment", {}).get("entity", {})
        pid = entity.get("id") or e.get("remote_id")
        if pid and pid not in seen:
            seen[pid] = {"payment_id": pid, "order_id": entity.get("order_id") or ""}
    return list(seen.values())


async def send_client_verifications(
    payments: list[dict],
    *,
    url: str,
    key_secret: str,
    fmt: str = "json",
    concurrency: int = 25,
    timeout_s: float = 5.0,
) -> list[dict]:
    """POST one checkout-style verify request per payment. Returns per-call results."""
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def one(client: httpx.AsyncClient, p: dict) -> dict:
        body = {
            "razorpay_order_id": p["order_id"],
            "razorpay_payment_id": p["payment_id"],
            "razorpay_signature": razorpay_checkout_signature(p["order_id"], p["payment_id"], key_secret),
        }
        async with sem:
            try:
                if fmt == "form":
                    resp = await client.post(url, data=body, timeout=timeout_s)
                else:
                    resp = await client.post(url, json=body, timeout=timeout_s)
                return {"payment_id": p["payment_id"], "status_code": resp.status_code, "ok": 200 <= resp.status_code < 300, "error": None}
            except Exception as e:  # network errors are results, not crashes
                return {"payment_id": p["payment_id"], "status_code": None, "ok": False, "error": str(e)}

    async with httpx.AsyncClient() as client:
        return list(await asyncio.gather(*[one(client, p) for p in payments]))

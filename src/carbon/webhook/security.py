"""Webhook security checks: attack the handler directly and score how it responds.

Each check sends a small batch of requests and grades the status codes. Unlike
`carbon run`, no scenario is needed: one sample payment event is built locally per
provider. A check's outcome is one of pass, fail, n/a (the provider has no such
mechanism) or inconclusive (the handler accepted no valid webhook at all, so a
rejection proves nothing).
"""

from __future__ import annotations

import copy
import hmac
import hashlib
import json
import time
import uuid
from typing import Any, Optional

import httpx

from carbon.webhook.client_verify import razorpay_checkout_signature
from carbon.webhook.payloads import build_event_payload, entity_to_event_type
from carbon.webhook.sender import TIMESTAMPED_PROVIDERS, _apply_signature_mode, sign

# How far outside the window the stale/future timestamps are. Stripe's default tolerance
# is 300s and Cashfree tells merchants to reject anything older than 5 minutes.
TIMESTAMP_SKEW_S = 600
DEFAULT_SECRETS = ("", "secret", "test", "whsec_test")
WEIGHTS = {"critical": 10, "high": 5}
# A single accepted forgery means anyone can fake a payment, so the score is capped.
CRITICAL_FAIL_CAP = 40

_NON_ASCII_NOTE = "Zoë's ₹499 plan — café"


def _is_2xx(sc: Optional[int]) -> bool:
    return isinstance(sc, int) and 200 <= sc < 300


def _is_4xx(sc: Optional[int]) -> bool:
    return isinstance(sc, int) and 400 <= sc < 500


def _contains_non_ascii(obj: Any) -> bool:
    return any(ord(c) > 127 for c in json.dumps(obj, ensure_ascii=False))


def sample_event(provider: str, amount: int = 49900) -> dict:
    """A payment-captured event in the provider's format, with a non-ASCII string in it."""
    suffix = uuid.uuid4().hex[:14]
    payload = build_event_payload(
        event_type=entity_to_event_type("payment", "captured", provider=provider) or "payment.captured",
        entity_type="payment",
        remote_id=f"pay_sec{suffix}",
        metadata={
            "amount": amount,
            "currency": "INR",
            "status": "captured",
            "order_id": f"order_sec{suffix}",
            "description": _NON_ASCII_NOTE,
        },
        provider=provider,
    )
    if not _contains_non_ascii(payload):
        payload["notes"] = {"description": _NON_ASCII_NOTE}
    return payload


def compact(payload: Any) -> bytes:
    """How gateways send JSON: no whitespace, keys in order, raw UTF-8."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def body_variants(payload: dict) -> dict[str, bytes]:
    """The same JSON document serialized differently. Each one is a valid webhook."""
    reordered = dict(reversed(list(payload.items())))
    return {
        "pretty-printed (whitespace)": json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
        "keys in a different order": json.dumps(reordered, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        ),
        "unicode escapes (\\u00eb)": json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8"),
    }


def tamper_amounts(payload: Any, factor: int = 100) -> Any:
    """Copy of payload with every numeric amount field multiplied by factor."""
    out = copy.deepcopy(payload)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if "amount" in k.lower() and isinstance(v, (int, float)) and not isinstance(v, bool):
                    node[k] = v * factor
                elif "amount" in k.lower() and isinstance(v, str) and v.isdigit():
                    node[k] = str(int(v) * factor)
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(out)
    return out


def _now_ts(provider: str, offset_s: int = 0) -> str:
    unit = TIMESTAMPED_PROVIDERS[provider]
    t = time.time() + offset_s
    return str(int(t * 1000)) if unit == "ms" else str(int(t))


class _Client:
    """Posts requests and records status codes (None for no response)."""

    def __init__(self, client: httpx.AsyncClient, timeout_s: float):
        self.client = client
        self.timeout_s = timeout_s
        self.errors: list[str] = []

    async def post(self, url: str, *, content: Optional[bytes] = None, headers=None, json_body=None) -> Optional[int]:
        try:
            if json_body is not None:
                resp = await self.client.post(url, json=json_body, timeout=self.timeout_s)
            else:
                resp = await self.client.post(url, content=content, headers=headers, timeout=self.timeout_s)
            return resp.status_code
        except httpx.HTTPError as e:
            self.errors.append(f"{type(e).__name__}: {e}")
            return None


def _check(
    check_id: str,
    name: str,
    severity: str,
    expected: str,
    statuses: list,
    fix: str,
    *,
    category: str = "security",
    status: Optional[str] = None,
    message: str = "",
) -> dict:
    """Grade a batch: expected 'reject' means every response must be 4xx; 'accept' means 2xx."""
    if status is None:
        if expected == "reject":
            status = "pass" if statuses and all(_is_4xx(s) for s in statuses) else "fail"
        else:
            status = "pass" if statuses and all(_is_2xx(s) for s in statuses) else "fail"
    if not message:
        accepted = sum(1 for s in statuses if _is_2xx(s))
        if status == "pass":
            message = (
                f"rejected all {len(statuses)}" if expected == "reject" else f"accepted all {len(statuses)}"
            )
        elif expected == "reject" and accepted:
            message = f"ACCEPTED {accepted} of {len(statuses)}"
        elif expected == "reject":
            message = f"{len(statuses) - accepted} of {len(statuses)} got 5xx or no response instead of 4xx"
        else:
            message = f"rejected {len(statuses) - accepted} of {len(statuses)} valid webhooks"
    return {
        "id": check_id,
        "name": name,
        "category": category,
        "severity": severity,
        "expected": "4xx" if expected == "reject" else "2xx",
        "statuses": statuses,
        "status": status,
        "passed": status == "pass",
        "message": message,
        "fix": fix if status == "fail" else "",
    }


async def _webhook_checks(c: _Client, url: str, provider: str, secret: str) -> list[dict]:
    payload = sample_event(provider)
    body = compact(payload)
    checks: list[dict] = []

    async def send(b: bytes, key: str, mode: str = "valid", timestamp: Optional[str] = None) -> Optional[int]:
        headers = _apply_signature_mode(sign(provider, b, key, timestamp), mode)
        return await c.post(url, content=b, headers=headers)

    # Valid webhooks first: the same document in several byte forms, each signed exactly.
    baseline = await send(body, secret)
    variant_statuses = {label: await send(b, secret) for label, b in body_variants(payload).items()}
    valid_accepted = _is_2xx(baseline) or any(_is_2xx(s) for s in variant_statuses.values())

    checks.append(_check(
        "valid_signature", "Valid signature accepted", "high", "accept", [baseline],
        "Your handler rejected a webhook signed with --webhook-secret. Check the secret matches "
        "the handler's, and that it verifies the raw request body.",
        category="correctness",
    ))

    if provider == "juspay":
        checks.append(_check(
            "raw_body", "Raw body (re-serialized JSON)", "high", "accept", [],
            "", category="correctness", status="n/a",
            message="Juspay authenticates with Basic Auth; the body is not signed",
        ))
    else:
        rejected = [label for label, s in variant_statuses.items() if not _is_2xx(s)]
        checks.append(_check(
            "raw_body", "Raw body (re-serialized JSON)", "high", "accept",
            list(variant_statuses.values()),
            "Compute the HMAC over the exact request bytes (Flask request.get_data(), Django "
            "request.body, Express express.raw()), never over json.dumps/JSON.stringify of the "
            "parsed body: re-serializing changes whitespace, key order and escapes.",
            category="correctness",
            message=(
                f"REJECTS VALID WEBHOOKS sent as: {', '.join(rejected)}. The signature is checked "
                "against re-serialized JSON, not the raw body"
                if rejected
                else f"accepted all {len(variant_statuses)} byte-different forms of the same webhook"
            ),
        ))

    rejection_checks: list[tuple] = []

    async def batch(*sends) -> list:
        return [await s for s in sends]

    rejection_checks.append((
        "missing_signature", "Unsigned", "critical",
        await batch(send(body, secret, "missing")),
        "Reject the request (401) when the signature header is absent; never skip verification.",
    ))
    rejection_checks.append((
        "corrupted_signature", "Corrupted signature", "critical",
        await batch(send(body, secret, "corrupted")),
        "Compare the computed signature with the header using a constant-time compare "
        "(hmac.compare_digest) and reject on mismatch.",
    ))
    rejection_checks.append((
        "wrong_secret", "Wrong secret", "critical",
        await batch(send(body, "wrong_secret_" + secret)),
        "Verify against your endpoint's own webhook secret, loaded from config.",
    ))
    defaults = [s for s in DEFAULT_SECRETS if s != secret]
    rejection_checks.append((
        "default_secrets", "Empty/default secrets", "critical",
        await batch(*[send(body, s) for s in defaults]),
        "The handler is verifying with an empty or placeholder secret: fail startup when the "
        "webhook secret env var is unset instead of defaulting it.",
    ))
    if provider == "juspay":
        checks.append(_check(
            "tampered_body", "Tampered amount", "critical",
            "reject", [], "", status="n/a", message="Juspay Basic Auth does not cover the body",
        ))
    else:
        tampered = compact(tamper_amounts(payload))
        headers = sign(provider, body, secret)  # valid signature for the original body
        rejection_checks.append((
            "tampered_body", "Tampered amount", "critical",
            [await c.post(url, content=tampered, headers=headers)],
            "Verify the signature over the body you then parse and act on; a valid signature for "
            "another body must not authorize this one.",
        ))

    if provider in TIMESTAMPED_PROVIDERS:
        rejection_checks.append((
            "stale_timestamp", f"Stale timestamp (-{TIMESTAMP_SKEW_S // 60} min)",
            "high",
            await batch(send(body, secret, timestamp=_now_ts(provider, -TIMESTAMP_SKEW_S))),
            "Reject when the signed timestamp is more than 5 minutes from now (Stripe's SDKs do this "
            "with tolerance=300); otherwise a captured webhook can be replayed forever.",
        ))
        rejection_checks.append((
            "future_timestamp", f"Future timestamp (+{TIMESTAMP_SKEW_S // 60} min)",
            "high",
            await batch(send(body, secret, timestamp=_now_ts(provider, TIMESTAMP_SKEW_S))),
            "Apply the tolerance in both directions: abs(now - timestamp) <= 300 seconds.",
        ))
    else:
        for cid, name in (("stale_timestamp", "Stale timestamp"),
                          ("future_timestamp", "Future timestamp")):
            checks.append(_check(cid, name, "high", "reject", [], "", status="n/a",
                                 message="no signed timestamp"))

    for cid, name, severity, statuses, fix in rejection_checks:
        if valid_accepted:
            checks.append(_check(cid, name, severity, "reject", statuses, fix))
        else:
            checks.append(_check(
                cid, name, severity, "reject", statuses, "", status="inconclusive",
                message="handler accepted no valid webhook, so rejecting this proves nothing",
            ))
    return checks


async def _client_verify_checks(c: _Client, url: str, key_secret: str) -> list[dict]:
    """Razorpay Checkout verify endpoint: razorpay_signature = HMAC(order_id|payment_id)."""
    suffix = uuid.uuid4().hex[:10]
    order_a, pay_a = f"order_A{suffix}", f"pay_A{suffix}"
    order_b, pay_b = f"order_B{suffix}", f"pay_B{suffix}"

    def body(order_id: str, payment_id: str, signature: str) -> dict:
        return {"razorpay_order_id": order_id, "razorpay_payment_id": payment_id, "razorpay_signature": signature}

    valid = await c.post(url, json_body=body(order_a, pay_a, razorpay_checkout_signature(order_a, pay_a, key_secret)))
    forged_sig = hmac.new(b"forged", f"{order_a}|{pay_a}".encode(), hashlib.sha256).hexdigest()
    forged = [
        await c.post(url, json_body=body(order_a, pay_a, forged_sig)),
        await c.post(url, json_body=body(order_a, pay_a, "0" * 64)),
    ]
    sig_b = razorpay_checkout_signature(order_b, pay_b, key_secret)
    # ids swapped between the two fields, signature valid for the unswapped pair
    swapped = [await c.post(url, json_body=body(pay_b, order_b, sig_b))]
    # a real payment id + signature from order B presented against order A
    other_order = [await c.post(url, json_body=body(order_a, pay_b, sig_b))]

    checks = [_check(
        "client_verify_valid", "Client verify: valid", "high", "accept", [valid],
        "Your verify endpoint rejected a correct razorpay_signature. Pass the API key secret as "
        "--client-verify-secret.",
        category="correctness",
    )]
    fix = (
        "Recompute HMAC_SHA256(key_secret, order_id + '|' + payment_id) using the order id you stored "
        "for this checkout (not the one the browser sends) and compare_digest it with razorpay_signature "
        "before crediting."
    )
    for cid, name, statuses in (
        ("client_verify_forged", "Client verify: forged signature", forged),
        ("client_verify_swapped", "Client verify: swapped ids", swapped),
        ("client_verify_other_order", "Client verify: other order's payment", other_order),
    ):
        if _is_2xx(valid):
            checks.append(_check(cid, name, "critical", "reject", statuses, fix))
        else:
            checks.append(_check(
                cid, name, "critical", "reject", statuses, "", status="inconclusive",
                message="endpoint accepted no valid request, so rejecting this proves nothing",
            ))
    return checks


def score(checks: list[dict]) -> int:
    """0-100: weighted share of graded checks passed, capped when a critical check fails."""
    graded = [c for c in checks if c["status"] in ("pass", "fail", "inconclusive")]
    total = sum(WEIGHTS.get(c["severity"], 1) for c in graded)
    if not total:
        return 0
    got = sum(WEIGHTS.get(c["severity"], 1) for c in graded if c["status"] == "pass")
    value = round(100 * got / total)
    if any(c["severity"] == "critical" and c["status"] == "fail" for c in checks):
        value = min(value, CRITICAL_FAIL_CAP)
    return value


def critical_failures(checks: list[dict]) -> list[dict]:
    return [c for c in checks if c["severity"] == "critical" and c["status"] == "fail"]


async def run_security_checks(
    *,
    provider: str,
    webhook_url: str,
    secret: str,
    client_verify_url: Optional[str] = None,
    client_verify_secret: Optional[str] = None,
    timeout_s: float = 5.0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> dict:
    """Run every applicable check and return the scorecard."""
    provider = "razorpay" if provider == "mock" else provider
    async with httpx.AsyncClient(transport=transport) as http:
        c = _Client(http, timeout_s)
        checks = await _webhook_checks(c, webhook_url, provider, secret)
        if client_verify_url:
            checks += await _client_verify_checks(c, client_verify_url, client_verify_secret or secret)
    sent = sum(len(ch["statuses"]) for ch in checks)
    connect_errors = [e for e in c.errors if e.startswith("ConnectError")]
    crit = critical_failures(checks)
    return {
        "provider": provider,
        "webhook_url": webhook_url,
        "client_verify_url": client_verify_url,
        "score": score(checks),
        "passed": not crit and not any(ch["status"] in ("fail", "inconclusive") for ch in checks),
        "critical_failures": [ch["id"] for ch in crit],
        "checks": checks,
        "requests_sent": sent,
        "nothing_listening": bool(sent) and len(connect_errors) == sent,
    }

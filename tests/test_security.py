"""`carbon security`: every attack graded against handlers that get it right and wrong."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import socket
import time

import httpx
import pytest
import respx
from typer.testing import CliRunner

from carbon.cli import app
from carbon.webhook.client_verify import razorpay_checkout_signature
from carbon.webhook.security import (
    CRITICAL_FAIL_CAP,
    body_variants,
    compact,
    run_security_checks,
    sample_event,
    score,
    tamper_amounts,
)

try:
    runner = CliRunner(mix_stderr=False)
except TypeError:
    runner = CliRunner()

SECRET = "s3cret"
URL = "http://handler.test/webhooks"
VERIFY_URL = "http://handler.test/payments/verify"
CRITICAL = {"missing_signature", "corrupted_signature", "wrong_secret", "default_secrets", "tampered_body"}


# --- handlers (httpx.Request -> httpx.Response) -------------------------------------------


def _hex(secret: str, msg: bytes, algo=hashlib.sha256) -> str:
    return hmac.new(secret.encode(), msg, algo).hexdigest()


def accept_all(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200)


def reject_all(request: httpx.Request) -> httpx.Response:
    return httpx.Response(401)


def razorpay_raw(request: httpx.Request, secrets=(SECRET,)) -> httpx.Response:
    sig = request.headers.get("X-Razorpay-Signature", "")
    ok = any(hmac.compare_digest(_hex(s, request.content), sig) for s in secrets)
    return httpx.Response(200 if ok else 401)


def razorpay_reserialized(request: httpx.Request) -> httpx.Response:
    """The common bug: HMAC over JSON.stringify(parsed) instead of the raw bytes."""
    body = json.dumps(json.loads(request.content), separators=(",", ":"), ensure_ascii=False).encode()
    ok = hmac.compare_digest(_hex(SECRET, body), request.headers.get("X-Razorpay-Signature", ""))
    return httpx.Response(200 if ok else 401)


def razorpay_crashes_unsigned(request: httpx.Request) -> httpx.Response:
    if "X-Razorpay-Signature" not in request.headers:
        return httpx.Response(500)
    return razorpay_raw(request)


def stripe_handler(tolerance=300):
    def handle(request: httpx.Request) -> httpx.Response:
        parts = dict(kv.split("=", 1) for kv in request.headers.get("Stripe-Signature", "").split(",") if "=" in kv)
        if "t" not in parts or "v1" not in parts:
            return httpx.Response(400)
        ok = hmac.compare_digest(_hex(SECRET, parts["t"].encode() + b"." + request.content), parts["v1"])
        if ok and tolerance is not None and abs(time.time() - int(parts["t"])) > tolerance:
            ok = False
        return httpx.Response(200 if ok else 400)

    return handle


def cashfree_handler(request: httpx.Request) -> httpx.Response:
    ts = request.headers.get("x-webhook-timestamp", "0")
    expected = base64.b64encode(
        hmac.new(SECRET.encode(), ts.encode() + request.content, hashlib.sha256).digest()
    ).decode()
    ok = hmac.compare_digest(expected, request.headers.get("x-webhook-signature", ""))
    ok = ok and abs(time.time() * 1000 - int(ts)) <= 300_000
    return httpx.Response(200 if ok else 401)


def hyperswitch_handler(request: httpx.Request) -> httpx.Response:
    sig = request.headers.get("X-Webhook-Signature-512", "")
    ok = hmac.compare_digest(_hex(SECRET, request.content, hashlib.sha512), sig)
    return httpx.Response(200 if ok else 401)


def juspay_handler(request: httpx.Request) -> httpx.Response:
    expected = "Basic " + base64.b64encode(f"{SECRET}:".encode()).decode()
    return httpx.Response(200 if request.headers.get("Authorization") == expected else 401)


def verify_correct(request: httpx.Request) -> httpx.Response:
    p = json.loads(request.content)
    expected = razorpay_checkout_signature(p["razorpay_order_id"], p["razorpay_payment_id"], SECRET)
    return httpx.Response(200 if hmac.compare_digest(expected, p["razorpay_signature"]) else 400)


def verify_length_only(request: httpx.Request) -> httpx.Response:
    """Bug: checks a signature is present and looks like hex, never recomputes it."""
    return httpx.Response(200 if len(json.loads(request.content)["razorpay_signature"]) == 64 else 400)


def routed(webhook, verify=None):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/payments/verify" and verify:
            return verify(request)
        return webhook(request)

    return handle


async def _card(provider, handler, **kw):
    return await run_security_checks(
        provider=provider, webhook_url=URL, secret=SECRET, transport=httpx.MockTransport(handler), **kw
    )


def _by_id(card) -> dict:
    return {c["id"]: c for c in card["checks"]}


# --- scoring against whole handlers -----------------------------------------------------


async def test_accept_all_fails_every_critical_check():
    card = await _card("razorpay", routed(accept_all, accept_all), client_verify_url=VERIFY_URL)
    checks = _by_id(card)
    assert set(card["critical_failures"]) == CRITICAL | {
        "client_verify_forged", "client_verify_swapped", "client_verify_other_order",
    }
    assert checks["missing_signature"]["message"].startswith("ACCEPTED")
    assert checks["missing_signature"]["fix"]
    assert card["score"] <= CRITICAL_FAIL_CAP
    assert not card["passed"]


async def test_correct_raw_body_handler_scores_100():
    card = await _card("razorpay", routed(razorpay_raw, verify_correct), client_verify_url=VERIFY_URL)
    assert card["score"] == 100
    assert card["passed"]
    assert all(c["fix"] == "" for c in card["checks"])
    # 1 baseline + 3 variants + missing/corrupted/wrong + 4 defaults + tampered + 5 client verify
    assert card["requests_sent"] == 1 + 3 + 3 + 4 + 1 + 5


async def test_reserialized_json_handler_flagged_as_rejecting_valid_webhooks():
    card = await _card("razorpay", razorpay_reserialized)
    raw = _by_id(card)["raw_body"]
    assert raw["status"] == "fail"
    assert raw["category"] == "correctness"
    assert "REJECTS VALID WEBHOOKS" in raw["message"]
    assert "raw body" in raw["message"] and "pretty-printed" in raw["message"]
    assert "request.get_data()" in raw["fix"]
    assert card["critical_failures"] == []
    assert 50 < card["score"] < 100


async def test_reject_all_makes_rejection_checks_inconclusive():
    card = await _card("razorpay", reject_all)
    checks = _by_id(card)
    assert checks["valid_signature"]["status"] == "fail"
    assert "--webhook-secret" in checks["valid_signature"]["fix"]
    for cid in CRITICAL:
        assert checks[cid]["status"] == "inconclusive"
    assert card["score"] == 0


async def test_5xx_on_unsigned_is_a_failure_but_not_acceptance():
    card = await _card("razorpay", razorpay_crashes_unsigned)
    missing = _by_id(card)["missing_signature"]
    assert missing["status"] == "fail"
    assert missing["statuses"] == [500]
    assert "5xx" in missing["message"]


async def test_default_secret_fallback_is_critical():
    card = await _card("razorpay", lambda r: razorpay_raw(r, secrets=(SECRET, "")))
    checks = _by_id(card)
    assert checks["default_secrets"]["status"] == "fail"
    assert checks["default_secrets"]["statuses"].count(200) == 1
    assert card["critical_failures"] == ["default_secrets"]
    assert card["score"] <= CRITICAL_FAIL_CAP


def test_default_secrets_skip_the_real_secret():
    import asyncio

    seen = []

    def handler(request):
        seen.append(request.headers.get("X-Razorpay-Signature"))
        return razorpay_raw(request)

    asyncio.run(run_security_checks(
        provider="razorpay", webhook_url=URL, secret="test", transport=httpx.MockTransport(handler),
    ))
    # '', 'secret', 'whsec_test' only; signing with 'test' would be a valid webhook
    assert len(seen) == 1 + 3 + 3 + 3 + 1


# --- timestamps (SEC-01) ---------------------------------------------------------------


async def test_stripe_with_tolerance_passes_and_without_is_flagged():
    good = await _card("stripe", stripe_handler(300))
    assert good["score"] == 100
    bad = await _card("stripe", stripe_handler(None))
    checks = _by_id(bad)
    assert checks["stale_timestamp"]["status"] == "fail"
    assert checks["future_timestamp"]["status"] == "fail"
    assert "300" in checks["stale_timestamp"]["fix"]
    assert bad["critical_failures"] == []  # replay window is high, not critical
    assert bad["score"] < 100


async def test_stale_stripe_timestamp_is_validly_signed_and_old():
    sent = []

    def handler(request):
        sent.append(request)
        return stripe_handler(300)(request)

    await _card("stripe", handler)
    stamps = [
        int(dict(kv.split("=", 1) for kv in r.headers["Stripe-Signature"].split(","))["t"])
        for r in sent if "Stripe-Signature" in r.headers
    ]
    now = time.time()
    assert any(now - t >= 600 for t in stamps) and any(t - now >= 590 for t in stamps)


async def test_cashfree_millisecond_timestamps():
    card = await _card("cashfree", cashfree_handler)
    checks = _by_id(card)
    assert checks["stale_timestamp"]["status"] == "pass"
    assert checks["future_timestamp"]["status"] == "pass"
    assert card["score"] == 100


@pytest.mark.parametrize("provider,handler", [("razorpay", razorpay_raw), ("hyperswitch", hyperswitch_handler)])
async def test_timestamps_na_without_signed_timestamp(provider, handler):
    card = await _card(provider, handler)
    checks = _by_id(card)
    assert checks["stale_timestamp"]["status"] == "n/a"
    assert checks["future_timestamp"]["status"] == "n/a"
    assert card["score"] == 100


async def test_juspay_basic_auth_marks_body_checks_na():
    card = await _card("juspay", juspay_handler)
    checks = _by_id(card)
    assert checks["tampered_body"]["status"] == "n/a"
    assert checks["raw_body"]["status"] == "n/a"
    assert checks["missing_signature"]["status"] == "pass"
    assert card["score"] == 100


# --- body construction (SEC-02, SEC-07) -------------------------------------------------


def test_body_variants_are_same_json_different_bytes():
    payload = sample_event("razorpay")
    variants = body_variants(payload)
    assert len(variants) == 3
    blobs = set(variants.values()) | {compact(payload)}
    assert len(blobs) == 4
    for b in variants.values():
        assert json.loads(b) == payload
    assert b"\\u00eb" in variants["unicode escapes (\\u00eb)"]


@pytest.mark.parametrize("provider", ["razorpay", "stripe", "cashfree", "hyperswitch"])
def test_sample_event_has_non_ascii_and_amounts(provider):
    payload = sample_event(provider)
    assert any(ord(c) > 127 for c in compact(payload).decode())
    assert compact(tamper_amounts(payload)) != compact(payload)


def test_tamper_amounts_multiplies_only_amounts():
    payload = {"amount": 100, "nested": {"payment_amount": 4.5, "amount_str": "200", "id": 7, "flag": True}}
    out = tamper_amounts(payload)
    assert out == {"amount": 10000, "nested": {"payment_amount": 450.0, "amount_str": "20000", "id": 7, "flag": True}}
    assert payload["amount"] == 100


async def test_tampered_body_carries_signature_for_original_body():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200)

    card = await _card("razorpay", handler)
    tampered_index = [c["id"] for c in card["checks"] if c["statuses"]]
    assert "tampered_body" in tampered_index
    original = requests[0]
    tampered = next(
        r for r in requests
        if json.loads(r.content).get("payload", {}).get("payment", {}).get("entity", {}).get("amount") == 4990000
    )
    assert tampered.headers["X-Razorpay-Signature"] == _hex(SECRET, original.content)


# --- client verify (SEC-09) -------------------------------------------------------------


async def test_client_verify_that_never_recomputes_is_critical():
    card = await _card("razorpay", routed(razorpay_raw, verify_length_only), client_verify_url=VERIFY_URL)
    checks = _by_id(card)
    assert checks["client_verify_forged"]["status"] == "fail"
    assert checks["client_verify_swapped"]["status"] == "fail"
    assert "client_verify_forged" in card["critical_failures"]


async def test_client_verify_uses_its_own_secret():
    def verify(request):
        p = json.loads(request.content)
        expected = razorpay_checkout_signature(p["razorpay_order_id"], p["razorpay_payment_id"], "key_secret")
        return httpx.Response(200 if expected == p["razorpay_signature"] else 400)

    card = await _card(
        "razorpay", routed(razorpay_raw, verify), client_verify_url=VERIFY_URL, client_verify_secret="key_secret"
    )
    assert card["score"] == 100


def test_score_weights_and_cap():
    def c(sev, status):
        return {"severity": sev, "status": status}

    assert score([c("high", "pass"), c("high", "n/a")]) == 100
    assert score([c("critical", "pass"), c("high", "fail")]) == 67
    assert score([c("critical", "fail")] + [c("critical", "pass")] * 9) == CRITICAL_FAIL_CAP
    assert score([c("high", "n/a")]) == 0


# --- CLI ------------------------------------------------------------------------------


def _cli(*args):
    return runner.invoke(app, ["security", "--webhook-url", URL, "--webhook-secret", SECRET, *args])


def test_cli_ci_exits_1_on_critical_failure():
    with respx.mock:
        respx.post(URL).mock(side_effect=accept_all)
        result = _cli("-p", "razorpay", "--ci")
    assert result.exit_code == 1, result.stdout
    assert "Security score" in result.stdout
    assert "CI check failed: missing_signature" in result.stdout + result.stderr


def test_cli_ci_passes_for_reserialized_handler_but_flags_it():
    with respx.mock:
        respx.post(URL).mock(side_effect=razorpay_reserialized)
        result = _cli("-p", "razorpay", "--ci")
    assert result.exit_code == 0, result.stdout
    assert "raw_body: Compute the HMAC over the exact request bytes" in result.stdout


def test_cli_ci_exits_1_when_nothing_valid_is_accepted():
    with respx.mock:
        respx.post(URL).mock(side_effect=reject_all)
        result = _cli("--ci")
    assert result.exit_code == 1


def test_cli_json_is_one_document_on_stdout():
    with respx.mock:
        respx.post(URL).mock(side_effect=stripe_handler(300))
        result = _cli("-p", "stripe", "--output", "json", "--ci")
    assert result.exit_code == 0, result.stderr
    doc = json.loads(result.stdout)
    assert doc["score"] == 100 and doc["passed"] is True
    assert doc["provider"] == "stripe"
    assert {"id", "severity", "expected", "statuses", "status", "fix"} <= set(doc["checks"][0])


def test_cli_client_verify_requires_razorpay():
    result = _cli("-p", "stripe", "--client-verify-url", VERIFY_URL)
    assert result.exit_code == 2


def test_cli_nothing_listening_exits_3():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    result = runner.invoke(
        app, ["security", "--webhook-url", f"http://127.0.0.1:{port}/w", "--webhook-secret", SECRET, "--ci"]
    )
    assert result.exit_code == 3
    assert "Nothing is listening" in result.stdout + result.stderr

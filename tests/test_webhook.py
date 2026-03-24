"""Unit tests for webhook payloads and sender (no DB)."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
import respx

from carbon.webhook.payloads import build_events_from_entity_map, entity_to_event_type
from carbon.webhook.sender import _apply_signature_mode, _post_one


class TestPayloadMapping:
    def test_entity_to_event_type(self):
        assert entity_to_event_type("payment", "authorized") == "payment.authorized"
        assert entity_to_event_type("payment", "captured") == "payment.captured"
        assert entity_to_event_type("payment", "failed") == "payment.failed"
        assert entity_to_event_type("dispute", "open") == "payment.dispute.created"
        assert entity_to_event_type("refund", "processed") == "refund.processed"
        assert entity_to_event_type("refund", "failed") == "refund.failed"
        assert entity_to_event_type("payment", "unknown") is None

    def test_build_events_from_entity_map(self):
        entity_map = {
            "payment_0": {
                "entity_type": "payment",
                "remote_id": "pay_123",
                "state": "captured",
                "metadata": {"amount": 100, "currency": "INR", "status": "captured"},
            },
            "refund_0": {
                "entity_type": "refund",
                "remote_id": "rfnd_123",
                "state": "processed",
                "metadata": {"amount": 100, "status": "processed"},
            },
            "noop_0": {
                "entity_type": "order",
                "remote_id": "order_123",
                "state": "created",
                "metadata": {},
            },
        }
        events = build_events_from_entity_map(entity_map, account_id="acc_test")
        assert len(events) == 2
        etypes = {e["event_type"] for e in events}
        assert "payment.captured" in etypes
        assert "refund.processed" in etypes
        for e in events:
            payload = e["payload"]
            assert payload["entity"] == "event"
            assert payload["account_id"] == "acc_test"
            assert payload["event"] == e["event_type"]
            assert "payload" in payload


@pytest.mark.asyncio
async def test_post_one_success():
    import httpx

    target = "http://example.test/webhook"
    event = {
        "event_type": "payment.captured",
        "entity_type": "payment",
        "local_id": "payment_0",
        "remote_id": "pay_123",
        "payload": {"entity": "event", "event": "payment.captured"},
    }
    with respx.mock:
        route = respx.post(target).mock(return_value=httpx.Response(200, text="ok"))
        async with httpx.AsyncClient() as client:
            sem = asyncio.Semaphore(1)
            d = await _post_one(client, sem, target, event, timeout_s=1.0, secret="testsecret", provider="razorpay")
    assert route.called
    req = route.calls[0].request
    assert "X-Razorpay-Signature" in req.headers
    assert d["event_type"] == "payment.captured"
    assert d["status_code"] == 200
    assert d["ok"] is True
    assert d["error"] is None
    assert isinstance(d["sent_at"], datetime)
    assert d["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_post_one_failure():
    import httpx

    target = "http://example.test/webhook"
    event = {
        "event_type": "payment.captured",
        "entity_type": "payment",
        "local_id": "payment_0",
        "remote_id": "pay_123",
        "payload": {"entity": "event", "event": "payment.captured"},
    }
    with respx.mock:
        route = respx.post(target).mock(return_value=httpx.Response(500, text="err"))
        async with httpx.AsyncClient() as client:
            sem = asyncio.Semaphore(1)
            d = await _post_one(client, sem, target, event, timeout_s=1.0, secret="testsecret", provider="razorpay")
    assert route.called
    req = route.calls[0].request
    assert "X-Razorpay-Signature" in req.headers
    assert d["status_code"] == 500
    assert d["ok"] is False
    assert d["error"] is None


@pytest.mark.asyncio
async def test_post_one_missing_signature():
    """Feature 3: missing signature mode strips auth headers."""
    import httpx

    target = "http://example.test/webhook"
    event = {
        "event_type": "payment.captured",
        "entity_type": "payment",
        "local_id": "payment_0",
        "remote_id": "pay_123",
        "payload": {"entity": "event", "event": "payment.captured"},
    }
    with respx.mock:
        route = respx.post(target).mock(return_value=httpx.Response(401, text="unauthorized"))
        async with httpx.AsyncClient() as client:
            sem = asyncio.Semaphore(1)
            d = await _post_one(client, sem, target, event, timeout_s=1.0, secret="testsecret", provider="razorpay", signature_mode="missing")
    assert route.called
    req = route.calls[0].request
    assert "X-Razorpay-Signature" not in req.headers
    assert d["status_code"] == 401


@pytest.mark.asyncio
async def test_post_one_corrupted_signature():
    """Feature 3: corrupted signature mode appends garbage to signature."""
    import httpx

    target = "http://example.test/webhook"
    event = {
        "event_type": "payment.captured",
        "entity_type": "payment",
        "local_id": "payment_0",
        "remote_id": "pay_123",
        "payload": {"entity": "event", "event": "payment.captured"},
    }
    with respx.mock:
        route = respx.post(target).mock(return_value=httpx.Response(200, text="ok"))
        async with httpx.AsyncClient() as client:
            sem = asyncio.Semaphore(1)
            d = await _post_one(client, sem, target, event, timeout_s=1.0, secret="testsecret", provider="razorpay", signature_mode="corrupted")
    assert route.called
    req = route.calls[0].request
    sig = req.headers.get("X-Razorpay-Signature", "")
    assert sig.endswith("CORRUPTED")


@pytest.mark.asyncio
async def test_post_one_stores_payload():
    """Feature 4: _post_one returns payload for storage."""
    import httpx

    target = "http://example.test/webhook"
    event = {
        "event_type": "payment.captured",
        "entity_type": "payment",
        "local_id": "payment_0",
        "remote_id": "pay_123",
        "payload": {"entity": "event", "event": "payment.captured"},
    }
    with respx.mock:
        respx.post(target).mock(return_value=httpx.Response(200, text="ok"))
        async with httpx.AsyncClient() as client:
            sem = asyncio.Semaphore(1)
            d = await _post_one(client, sem, target, event, timeout_s=1.0, secret="testsecret", provider="razorpay")
    assert "payload" in d
    assert d["payload"] is not None


@pytest.mark.asyncio
async def test_post_one_with_attempt():
    """Feature 1: duplicate webhook includes attempt number."""
    import httpx

    target = "http://example.test/webhook"
    event = {
        "event_type": "payment.captured",
        "entity_type": "payment",
        "local_id": "payment_0",
        "remote_id": "pay_123",
        "payload": {"entity": "event", "event": "payment.captured"},
        "attempt": 3,
    }
    with respx.mock:
        respx.post(target).mock(return_value=httpx.Response(200, text="ok"))
        async with httpx.AsyncClient() as client:
            sem = asyncio.Semaphore(1)
            d = await _post_one(client, sem, target, event, timeout_s=1.0, secret="testsecret", provider="razorpay")
    assert d["attempt"] == 3


class TestSignatureMode:
    def test_valid_mode_passes_through(self):
        headers = {"Content-Type": "application/json", "X-Razorpay-Signature": "abc123"}
        result = _apply_signature_mode(dict(headers), "valid")
        assert result == headers

    def test_missing_mode_strips_signatures(self):
        headers = {"Content-Type": "application/json", "X-Razorpay-Signature": "abc123"}
        result = _apply_signature_mode(dict(headers), "missing")
        assert "X-Razorpay-Signature" not in result
        assert result.get("Content-Type") == "application/json"

    def test_corrupted_mode_appends_garbage(self):
        headers = {"Content-Type": "application/json", "X-Razorpay-Signature": "abc123"}
        result = _apply_signature_mode(dict(headers), "corrupted")
        assert result["X-Razorpay-Signature"] == "abc123CORRUPTED"

    def test_stripe_missing_mode(self):
        headers = {"Content-Type": "application/json", "Stripe-Signature": "t=123,v1=abc"}
        result = _apply_signature_mode(dict(headers), "missing")
        assert "Stripe-Signature" not in result


class TestNewScenarios:
    """Feature 6 & 7: verify new scenarios load and compile."""

    def test_upi_timeout_compiles(self):
        from carbon.compiler import compile_scenario
        from carbon.scenarios import load_scenario

        _, scenario = load_scenario("upi-timeout")
        plan = compile_scenario(scenario)
        assert len(plan.steps) > 0

    def test_vpa_not_found_compiles(self):
        from carbon.compiler import compile_scenario
        from carbon.scenarios import load_scenario

        _, scenario = load_scenario("vpa-not-found")
        plan = compile_scenario(scenario)
        assert len(plan.steps) > 0

    def test_mandate_rejection_compiles(self):
        from carbon.compiler import compile_scenario
        from carbon.scenarios import load_scenario

        _, scenario = load_scenario("mandate-rejection")
        plan = compile_scenario(scenario)
        assert len(plan.steps) > 0

    def test_settlement_delay_compiles(self):
        from carbon.compiler import compile_scenario
        from carbon.scenarios import load_scenario

        _, scenario = load_scenario("settlement-delay")
        plan = compile_scenario(scenario)
        assert len(plan.steps) > 0

    def test_all_scenarios_listed(self):
        from carbon.scenarios import list_scenarios

        names = list_scenarios()
        assert "upi-timeout" in names
        assert "vpa-not-found" in names
        assert "mandate-rejection" in names
        assert "settlement-delay" in names


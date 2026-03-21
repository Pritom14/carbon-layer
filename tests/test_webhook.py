"""Unit tests for webhook payloads and sender (no DB)."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
import respx

from carbon.webhook.payloads import build_events_from_entity_map, entity_to_event_type
from carbon.webhook.sender import _post_one


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


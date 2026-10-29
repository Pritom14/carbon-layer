"""Hyperswitch outgoing-webhook format and signing."""

from __future__ import annotations

import asyncio
import hashlib
import hmac

import httpx
import pytest
import respx

from carbon.webhook.payloads import build_events_from_entity_map, entity_to_event_type
from carbon.webhook.sender import _post_one, _sign_hyperswitch

ENTITY_MAP = {
    "payment_0": {
        "entity_type": "payment",
        "remote_id": "pay_abc",
        "state": "captured",
        "metadata": {"id": "pay_abc", "amount": 25000, "currency": "INR", "status": "captured"},
    },
    "refund_0": {
        "entity_type": "refund",
        "remote_id": "ref_1",
        "state": "processed",
        "metadata": {"payment_id": "pay_abc", "amount": 25000},
    },
    "dispute_0": {
        "entity_type": "dispute",
        "remote_id": "dp_1",
        "state": "open",
        "metadata": {"payment_id": "pay_abc"},
    },
}


def test_event_mapping():
    assert entity_to_event_type("payment", "captured", "hyperswitch") == "payment_succeeded"
    assert entity_to_event_type("payment", "failed", "hyperswitch") == "payment_failed"
    assert entity_to_event_type("refund", "processed", "hyperswitch") == "refund_succeeded"
    assert entity_to_event_type("dispute", "open", "hyperswitch") == "dispute_opened"


def test_envelope_shape():
    events = {e["event_type"]: e["payload"] for e in build_events_from_entity_map(ENTITY_MAP, provider="hyperswitch")}
    pay = events["payment_succeeded"]
    assert set(pay) >= {"merchant_id", "event_id", "event_type", "content", "timestamp"}
    assert pay["content"]["type"] == "payment_details"
    assert pay["content"]["object"]["payment_id"] == "pay_abc"
    assert pay["content"]["object"]["status"] == "succeeded"
    assert pay["content"]["object"]["amount"] == 25000

    refund = events["refund_succeeded"]["content"]
    assert refund["type"] == "refund_details"
    assert refund["object"]["payment_id"] == "pay_abc" and refund["object"]["status"] == "succeeded"

    dispute = events["dispute_opened"]["content"]
    assert dispute["type"] == "dispute_details"
    assert dispute["object"]["dispute_status"] == "dispute_opened"
    assert isinstance(dispute["object"]["amount"], str)


def test_signature_is_hmac_sha512_hex():
    body = b'{"event_type":"payment_succeeded"}'
    headers = _sign_hyperswitch(body, "hash_key")
    expected = hmac.new(b"hash_key", body, hashlib.sha512).hexdigest()
    assert headers["X-Webhook-Signature-512"] == expected


@pytest.mark.asyncio
async def test_signature_matches_sent_body():
    target = "http://example.test/hs"
    event = build_events_from_entity_map(ENTITY_MAP, provider="hyperswitch")[0]
    with respx.mock:
        route = respx.post(target).mock(return_value=httpx.Response(200))
        async with httpx.AsyncClient() as client:
            await _post_one(client, asyncio.Semaphore(1), target, event, 1.0, "k", "hyperswitch")
    req = route.calls[0].request
    expected = hmac.new(b"k", req.content, hashlib.sha512).hexdigest()
    assert req.headers["X-Webhook-Signature-512"] == expected

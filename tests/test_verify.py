"""Side-effect verification, Razorpay event-id header, and parameter references."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from carbon.compiler import compile_scenario
from carbon.compiler.compiler import render_expected
from carbon.scenarios.registry import load_scenario
from carbon.webhook.sender import _apply_signature_mode, _post_one
from carbon.webhook.verify import VerifyError, evaluate, read_counter


def _deliveries(event_ids, ok=True, event_type="payment.captured"):
    return [{"event_type": event_type, "ok": ok, "provider_event_id": e} for e in event_ids]


class TestEvaluate:
    def test_exactly_once_passes(self):
        d = _deliveries(["a", "a", "b", "b"])
        v = evaluate(10, 12, d, "payment.captured")
        assert v["passed"] and v["verdict"] == "pass"
        assert v["unique_events_acked"] == 2 and v["deliveries"] == 4

    def test_duplicates_detected(self):
        d = _deliveries(["a", "a", "a"])
        v = evaluate(0, 3, d, "payment.captured")
        assert not v["passed"] and v["verdict"] == "duplicates"

    def test_lost_events_detected(self):
        d = _deliveries(["a", "b", "c"])
        v = evaluate(0, 1, d, "payment.captured")
        assert not v["passed"] and v["verdict"] == "lost"

    def test_rejected_deliveries_not_expected(self):
        d = _deliveries(["a", "b"], ok=False)
        v = evaluate(5, 5, d, "payment.captured")
        assert v["passed"] and v["unique_events_acked"] == 0

    def test_other_event_types_ignored(self):
        d = _deliveries(["a"]) + _deliveries(["r1", "r2"], event_type="refund.processed")
        assert evaluate(0, 1, d, "payment.captured")["passed"]


class TestReadCounter:
    def test_reads_last_line(self):
        assert read_counter("echo header; echo 42") == 42

    def test_non_numeric_raises(self):
        with pytest.raises(VerifyError):
            read_counter("echo nope")

    def test_failing_command_raises(self):
        with pytest.raises(VerifyError):
            read_counter("exit 3")


@pytest.mark.asyncio
async def test_razorpay_event_id_header_sent():
    target = "http://example.test/webhook"
    event = {
        "event_type": "payment.captured",
        "provider_event_id": "evt_fixed123",
        "payload": {"entity": "event", "event": "payment.captured"},
    }
    with respx.mock:
        route = respx.post(target).mock(return_value=httpx.Response(200))
        async with httpx.AsyncClient() as client:
            d = await _post_one(client, asyncio.Semaphore(1), target, event, 1.0, "s", "razorpay")
    assert route.calls[0].request.headers["X-Razorpay-Event-Id"] == "evt_fixed123"
    assert d["provider_event_id"] == "evt_fixed123"


def test_signature_modes_keep_event_id():
    h = {"Content-Type": "application/json", "X-Razorpay-Signature": "abc", "X-Razorpay-Event-Id": "evt_1"}
    corrupted = _apply_signature_mode(dict(h), "corrupted")
    assert corrupted["X-Razorpay-Event-Id"] == "evt_1"
    assert corrupted["X-Razorpay-Signature"].endswith("CORRUPTED")
    missing = _apply_signature_mode(dict(h), "missing")
    assert "X-Razorpay-Signature" not in missing and missing["X-Razorpay-Event-Id"] == "evt_1"


class TestParameterReferences:
    def test_set_override_changes_counts(self):
        _, scenario = load_scenario("dispute-spike")
        plan = compile_scenario(scenario, overrides={"baseline_orders": 200, "dispute_rate": 0.5})
        counts = [s.params["count"] for s in plan.steps if "count" in s.params]
        assert counts == [200, 100]

    def test_defaults_unchanged(self):
        _, scenario = load_scenario("dispute-spike")
        plan = compile_scenario(scenario)
        assert [s.params["count"] for s in plan.steps if "count" in s.params] == [1000, 150]

    def test_derived_rate(self):
        _, scenario = load_scenario("upi-timeout")
        plan = compile_scenario(scenario, overrides={"timeout_rate": 0.4})
        rates = [s.params["success_rate"] for s in plan.steps if "success_rate" in s.params]
        assert rates == [pytest.approx(0.6)]

    def test_render_expected(self):
        assert render_expected(">= ${baseline_orders * dispute_rate}", {"baseline_orders": 1000, "dispute_rate": 0.15}) == ">= 150"
        assert render_expected("< 0.8", {}) == "< 0.8"

    def test_unknown_reference_rejected(self):
        with pytest.raises(ValueError):
            render_expected(">= ${nope}", {})

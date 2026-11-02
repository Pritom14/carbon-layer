"""v0.12 failure modes: gateway retry outlook, late payment.failed, double payments,
concurrent duplicate deliveries, and late UPI captures.

The end-to-end tests run the real CLI against a tiny Razorpay webhook handler backed by
SQLite (one handler, with each bug switchable), and check its state with --verify-cmd.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shlex
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from typer.testing import CliRunner

from carbon.cli import app
from carbon.webhook import retry_policy
from carbon.webhook.checks import evaluate_deliveries
from carbon.webhook.payloads import build_events_from_entity_map
from carbon.webhook.retry_policy import POLICIES, client_timeout_s, outlook_finding, project
from carbon.webhook.verify import evaluate, evaluate_unchanged

try:  # click < 8.2 mixes stderr into stdout unless told otherwise
    runner = CliRunner(mix_stderr=False)
except TypeError:
    runner = CliRunner()

SECRET = "carbon"


def _text(result) -> str:
    """Everything printed, with rich's line wrapping undone."""
    return " ".join((result.stdout + result.stderr).split())


@pytest.fixture(autouse=True)
def _sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/carbon.db")
    for var in ("RAZORPAY_API_KEY", "RAZORPAY_API_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


# ---------------------------------------------------------------------------
# A tiny shop: Razorpay webhooks -> orders, payments, credits, refunds
# ---------------------------------------------------------------------------

SCHEMA = """
create table if not exists orders(id text primary key, status text);
create table if not exists payments(id text primary key, order_id text, status text);
create table if not exists credits(id integer primary key, order_id text, payment_id text);
create table if not exists refunds(payment_id text primary key);
"""


class Shop:
    """bugs: 'race' (check-then-insert credit), 'per_payment' (credit per payment, not per order),
    'regress' (a late payment.failed un-pays an order), 'ignore_cancelled' (a capture for a
    cancelled order is acked and dropped), 'error_captured' (500 on payment.captured),
    'slow_captured' (sleeps before answering payment.captured)."""

    def __init__(self, path, bugs=(), slow_s=0.0):
        self.path, self.bugs, self.slow_s = str(path), set(bugs), slow_s
        db = sqlite3.connect(self.path)
        db.executescript(SCHEMA)
        if "race" not in self.bugs:
            key = "payment_id" if "per_payment" in self.bugs else "order_id"
            db.execute(f"create unique index if not exists one_credit on credits({key})")
        db.commit()
        db.close()
        self.lock = threading.Lock()  # serializes state changes except the racy credit

    def db(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        return db

    def credit(self, db, order_id, payment_id):
        if "race" in self.bugs:
            # "Already credited?" then insert: two simultaneous deliveries both pass the check
            if db.execute("select 1 from credits where payment_id=?", (payment_id,)).fetchone():
                return
            time.sleep(0.05)
            db.execute("insert into credits(order_id, payment_id) values (?, ?)", (order_id, payment_id))
            return
        cur = db.execute(
            "insert or ignore into credits(order_id, payment_id) values (?, ?)", (order_id, payment_id)
        )
        if cur.rowcount == 0 and "per_payment" not in self.bugs:
            # The order was already credited by another payment: refund this one
            db.execute("insert or ignore into refunds values (?)", (payment_id,))

    def handle(self, event):
        entity = event["payload"]["payment"]["entity"]
        pid, oid, kind = entity["id"], entity["order_id"], event["event"]
        db = self.db()
        try:
            if kind == "payment.captured":
                if "race" in self.bugs:
                    self.credit(db, oid, pid)
                    db.execute("insert or replace into orders values (?, 'paid')", (oid,))
                    return
                with self.lock:
                    db.execute("insert or replace into payments values (?, ?, 'captured')", (pid, oid))
                    row = db.execute("select status from orders where id=?", (oid,)).fetchone()
                    if row and row[0] == "cancelled":
                        if "ignore_cancelled" in self.bugs:
                            return
                        # Late capture for a cancelled order: reinstate it
                    db.execute("insert or replace into orders values (?, 'paid')", (oid,))
                    self.credit(db, oid, pid)
            elif kind == "payment.failed":
                with self.lock:
                    db.execute("insert or ignore into payments values (?, ?, 'failed')", (pid, oid))
                    row = db.execute("select status from orders where id=?", (oid,)).fetchone()
                    if row and row[0] == "paid" and "regress" not in self.bugs:
                        return
                    db.execute("insert or replace into orders values (?, 'cancelled')", (oid,))
        finally:
            db.close()

    def serve(self):
        shop = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(sig, self.headers.get("X-Razorpay-Signature", "")):
                    return self._reply(401)
                event = json.loads(raw)
                if event["event"] == "payment.captured":
                    if "error_captured" in shop.bugs:
                        return self._reply(500)
                    if "slow_captured" in shop.bugs:
                        time.sleep(shop.slow_s)
                if event["event"].startswith("payment."):
                    shop.handle(event)
                self._reply(200)

            def _reply(self, code):
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.server = server
        return f"http://127.0.0.1:{server.server_address[1]}/webhooks"

    def count_cmd(self, sql):
        code = f"import sqlite3; print(sqlite3.connect({self.path!r}).execute({sql!r}).fetchone()[0])"
        return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


@pytest.fixture
def shop(tmp_path):
    started = []

    def make(bugs=(), slow_s=0.0):
        s = Shop(tmp_path / f"shop{len(started)}.db", bugs, slow_s)
        url = s.serve()
        started.append(s)
        return s, url

    yield make
    for s in started:
        s.server.shutdown()
        s.server.server_close()


def _carbon(*args):
    return runner.invoke(app, ["run", *args, "-p", "razorpay", "--webhook-secret", SECRET])


NOT_PAID = "select count(*) from orders where status != 'paid'"
CREDITED_TWICE = "select count(*) from (select order_id from credits group by order_id having count(*) > 1)"
CANCELLED_BUT_PAID = (
    "select count(*) from orders o join payments p on p.order_id = o.id where o.status = 'cancelled' "
    "and p.status = 'captured' and p.id not in (select payment_id from refunds)"
)


# ---------------------------------------------------------------------------
# RETRY-03: gateway retry outlook
# ---------------------------------------------------------------------------


def _d(event_type="payment.captured", status=200, ms=50, eid=None):
    return {"event_type": event_type, "status_code": status, "ok": status is not None and 200 <= status < 300,
            "duration_ms": ms, "provider_event_id": eid}


class TestRetryOutlook:
    def test_every_policy_cites_a_source(self):
        for policy in POLICIES.values():
            assert policy.source.startswith("https://")
            assert policy.retries

    def test_razorpay_disables_after_24_hours(self):
        o = project([_d(status=500, eid=f"e{i}") for i in range(4)], "razorpay")
        assert o["verdict"] == "disable" and o["disable_projected"] and not o["passed"]
        assert o["headline"] == "Razorpay would disable this endpoint after 24 hours"
        assert o["failure_rate"] == 1.0
        assert not any(line.startswith("Unconfirmed") for line in o["details"])

    def test_stripe_disable_is_labelled_unconfirmed(self):
        o = project([_d("payment_intent.succeeded", 500)], "stripe")
        assert o["headline"] == "Stripe would disable this endpoint after about 3 days"
        assert o["disable_confirmed"] is False
        assert any("several days" in line for line in o["details"] if line.startswith("Unconfirmed"))

    def test_slow_2xx_past_razorpay_timeout_is_a_failure(self):
        o = project([_d(ms=6100, eid="a"), _d(ms=5900, eid="b")], "razorpay")
        assert o["reasons"] == {"slow": 2}
        assert o["verdict"] == "disable"
        assert any("after Razorpay's 5 s timeout (slowest 6.1 s)" in line for line in o["details"])
        assert any("background job" in line for line in o["details"])

    def test_one_event_type_always_failing_is_persistent(self):
        ds = [_d("payment.captured", 500, eid=f"c{i}") for i in range(3)]
        ds += [_d("refund.processed", 200, eid=f"r{i}") for i in range(3)]
        o = project(ds, "razorpay")
        assert o["always_failing"] == ["payment.captured"]
        assert o["verdict"] == "disable" and o["failure_rate"] == 0.5

    def test_scattered_failures_are_left_to_retries(self):
        ds = [_d(status=500, eid="a"), _d(eid="b"), _d(eid="c"), _d(status=503, eid="d")]
        o = project(ds, "razorpay")
        assert o["verdict"] == "recoverable" and o["passed"]

    def test_event_failing_three_retries_is_persistent(self):
        ds = [_d(status=500, eid="a")] * 3 + [_d(eid="b")] * 3 + [_d(status=500, eid="c"), _d(eid="c"), _d(eid="c")]
        o = project(ds, "razorpay")
        assert o["verdict"] == "disable"
        assert any("1 events failed on all of their 3 or more deliveries" in line for line in o["details"])

    def test_two_failed_retries_are_not_yet_a_pattern(self):
        ds = [_d(status=500, eid="a")] * 2 + [_d(eid="b")] * 2
        assert project(ds, "razorpay")["verdict"] == "recoverable"

    @pytest.mark.parametrize("provider,words", [
        ("cashfree", "3 times, at 2, 10 and 30 minute intervals"),
        ("hyperswitch", "16 times over about 24 hours"),
    ])
    def test_gateways_without_documented_disable_stop_retrying(self, provider, words):
        o = project([_d(status=500)], provider)
        assert o["verdict"] == "stops_retrying" and not o["disable_projected"] and not o["passed"]
        assert words in o["headline"]

    def test_4xx_counts_as_failed_delivery(self):
        o = project([_d(status=400)], "razorpay")
        assert o["reasons"] == {"4xx": 1} and o["verdict"] == "disable"
        assert any("to the gateway a 4xx is a failed delivery" in line for line in o["details"])

    def test_healthy_endpoint(self):
        o = project([_d(), _d("refund.processed")], "razorpay")
        assert o["verdict"] == "ok" and o["passed"]
        assert "within the 5 s timeout" in o["headline"]

    def test_no_policy_for_juspay(self):
        o = project([_d("ORDER_SUCCEEDED", 500)], "juspay")
        assert o["verdict"] == "unknown" and o["passed"]

    def test_client_waits_past_gateway_timeout(self):
        assert client_timeout_s("razorpay") == 10.0
        assert client_timeout_s("stripe") == 5.0

    def test_finding(self):
        f = outlook_finding(project([_d(status=500)], "razorpay"))
        assert f["check"] == "retry_outlook" and not f["passed"]
        assert "always: payment.captured" in f["details"]["got"]

    def test_delivery_check_grades_slow_2xx(self):
        (f,) = evaluate_deliveries([_d(ms=6000), _d(ms=100)], timeout_s=5.0, gateway="Razorpay")
        assert not f["passed"] and "after Razorpay's 5 s timeout (1 too slow)" in f["message"]
        (f,) = evaluate_deliveries([_d(ms=6000)])
        assert f["passed"]

    def test_cli_ci_fails_when_disable_projected(self, shop):
        s, url = shop(bugs={"error_captured"})
        result = _carbon("flash-sale", "--set", "baseline_orders=4", "--webhook-url", url, "--ci")
        assert result.exit_code == 1, _text(result)
        out = _text(result)
        assert "Razorpay would disable this endpoint after 24 hours" in out
        assert "Source: https://razorpay.com/docs/webhooks/best-practices/" in out

    def test_cli_slow_handler(self, shop, monkeypatch):
        fast = retry_policy.RetryPolicy(**{**POLICIES["razorpay"].__dict__, "timeout_s": 0.2})
        monkeypatch.setitem(POLICIES, "razorpay", fast)
        s, url = shop(bugs={"slow_captured"}, slow_s=0.4)
        result = _carbon("flash-sale", "--set", "baseline_orders=3", "--webhook-url", url, "--output", "json")
        data = json.loads(result.stdout)
        assert data["webhooks"]["slow"] == 3 and data["webhooks"]["status_2xx"] == 3
        assert data["retry_outlook"]["verdict"] == "disable"
        assert data["retry_outlook"]["reasons"] == {"slow": 3}
        assert data["passed"] is False

    def test_cli_healthy_handler_passes(self, shop):
        s, url = shop()
        result = _carbon("flash-sale", "--set", "baseline_orders=4", "--webhook-url", url, "--ci")
        assert result.exit_code == 0, _text(result)
        assert "Razorpay would keep this endpoint enabled" in _text(result)

    def test_no_outlook_for_bad_signature_modes(self, shop):
        s, url = shop()
        result = _carbon("min-amount", "--webhook-url", url, "--webhook-signature", "missing", "--output", "json")
        assert json.loads(result.stdout)["retry_outlook"] is None


# ---------------------------------------------------------------------------
# FM-04: late payment.failed after payment.captured
# ---------------------------------------------------------------------------


def _late_failure_map():
    return {
        "payment_order_0": {"entity_type": "payment", "remote_id": "pay_1", "state": "captured",
                            "metadata": {"id": "pay_1", "order_id": "order_1", "status": "captured"}},
        "late_failed_payment_order_0": {
            "entity_type": "payment", "remote_id": "pay_1", "state": "failed",
            "metadata": {"id": "pay_1", "order_id": "order_1", "status": "failed",
                         "_carbon": {"stage": 2, "followup_of": "payment_order_0"}},
        },
    }


class TestLateFailure:
    def test_late_event_is_same_payment_in_later_stage(self):
        events = build_events_from_entity_map(_late_failure_map())
        by_type = {e["event_type"]: e for e in events}
        assert by_type["payment.captured"]["stage"] == 1
        assert by_type["payment.failed"]["stage"] == 2
        failed = by_type["payment.failed"]["payload"]["payload"]["payment"]["entity"]
        assert failed["id"] == "pay_1" and failed["status"] == "failed"
        assert "\"_carbon\"" not in json.dumps(by_type["payment.failed"]["payload"])

    def test_followups_do_not_count_as_payments(self):
        from carbon.validator.validator import _compute_metrics

        m = _compute_metrics(_late_failure_map())
        assert m["payments_attempted_count"] == 1 and m["payment_success_rate"] == 1.0

    def test_unchanged_expectation(self):
        v = evaluate_unchanged(0, 3, [], counts="orders not paid", fix="Ignore it")
        assert not v["passed"] and v["message"] == "3 new orders not paid" and v["fix"] == "Ignore it"
        assert evaluate_unchanged(2, 2, [])["passed"]
        assert evaluate_unchanged(2, 1, [])["passed"]

    def test_buggy_handler_regresses_paid_orders(self, shop):
        s, url = shop(bugs={"regress"})
        result = _carbon(
            "late-failure", "--set", "baseline_orders=8", "--set", "reorder_rate=0.25", "--set", "late_delay_s=0.1",
            "--webhook-url", url, "--verify-cmd", s.count_cmd(NOT_PAID), "--ci",
        )
        assert result.exit_code == 1, _text(result)
        out = _text(result)
        assert "Delivered in 3 batches" in out
        # 6 of 8 got payment.failed after the capture; the 2 reordered ones end paid
        assert "checker: 0 -> 6" in out
        assert "State check: unpaid orders (must not grow)" in out and "FAIL: 6 new unpaid orders" in out

    def test_fixed_handler_keeps_orders_paid(self, shop):
        s, url = shop()
        result = _carbon(
            "late-failure", "--set", "baseline_orders=8", "--set", "late_delay_s=0.1",
            "--webhook-url", url, "--verify-cmd", s.count_cmd(NOT_PAID), "--ci", "--output", "json",
        )
        assert result.exit_code == 0, _text(result)
        v = json.loads(result.stdout)["verification"]
        assert v["mode"] == "unchanged" and v["passed"] and v["after"] == 0

    def test_reordered_failure_first_needs_capture_to_win(self, shop):
        # Every payment.failed arrives first; a handler that drops captures for cancelled
        # orders leaves all of them unpaid
        s, url = shop(bugs={"ignore_cancelled"})
        result = _carbon(
            "late-failure", "--set", "baseline_orders=4", "--set", "reorder_rate=1", "--set", "late_delay_s=0.1",
            "--webhook-url", url, "--verify-cmd", s.count_cmd(NOT_PAID), "--output", "json",
        )
        v = json.loads(result.stdout)["verification"]
        assert v["observed_change"] == 4 and not v["passed"]


# ---------------------------------------------------------------------------
# FM-21: two payments for one order, and concurrent duplicate deliveries
# ---------------------------------------------------------------------------


class TestDoublePayment:
    def test_second_payment_shares_the_order(self):
        from carbon.compiler import compile_scenario
        from carbon.scenarios import load_scenario

        _, scenario = load_scenario("double-payment")
        assert scenario.verify["expect"] == "unchanged"
        plan = compile_scenario(scenario, overrides={"baseline_orders": 10})
        assert [s.action_type for s in plan.steps][-1] == "duplicate_payments"

    def test_buggy_handler_credits_order_twice(self, shop):
        s, url = shop(bugs={"per_payment"})
        result = _carbon(
            "double-payment", "--set", "baseline_orders=6", "--set", "double_pay_rate=0.5",
            "--webhook-url", url, "--verify-cmd", s.count_cmd(CREDITED_TWICE), "--ci",
        )
        assert result.exit_code == 1, _text(result)
        assert "checker: 0 -> 3" in _text(result)

    def test_fixed_handler_credits_once_and_refunds(self, shop):
        s, url = shop()
        result = _carbon(
            "double-payment", "--set", "baseline_orders=6", "--set", "double_pay_rate=0.5",
            "--webhook-url", url, "--verify-cmd", s.count_cmd(CREDITED_TWICE), "--ci",
        )
        assert result.exit_code == 0, _text(result)
        db = sqlite3.connect(s.path)
        assert db.execute("select count(*) from refunds").fetchone()[0] == 3

    def test_race_message_names_the_fix(self):
        d = [{"event_type": "payment.captured", "ok": True, "provider_event_id": "a"}] * 5
        v = evaluate(0, 5, d, "payment.captured", burst=5)
        assert "check-then-insert race" in v["message"] and "unique constraint" in v["message"]

    def test_concurrency_exposes_check_then_insert_race(self, shop):
        s, url = shop(bugs={"race"})
        result = _carbon(
            "flash-sale", "--set", "baseline_orders=4", "--webhook-url", url,
            "--webhook-concurrency", "5", "--verify-cmd", s.count_cmd("select count(*) from credits"), "--ci",
        )
        assert result.exit_code == 1, _text(result)
        out = _text(result)
        assert "Each webhook sent 5 times at the same instant (20 deliveries)" in out
        assert "check-then-insert race" in out

    def test_same_handler_passes_sequential_retries(self, shop):
        s, url = shop(bugs={"race"})
        result = _carbon(
            "flash-sale", "--set", "baseline_orders=4", "--webhook-url", url,
            "--webhook-repeat", "5", "--verify-cmd", s.count_cmd("select count(*) from credits"), "--ci",
        )
        assert result.exit_code == 0, _text(result)

    def test_unique_constraint_survives_concurrency(self, shop):
        s, url = shop()
        result = _carbon(
            "flash-sale", "--set", "baseline_orders=4", "--webhook-url", url,
            "--webhook-concurrency", "5", "--verify-cmd", s.count_cmd("select count(*) from credits"), "--ci",
        )
        assert result.exit_code == 0, _text(result)

    def test_concurrency_must_be_positive(self):
        result = _carbon("min-amount", "--webhook-concurrency", "0")
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# FM-27: UPI timeout, order cancelled, payment captured later
# ---------------------------------------------------------------------------


class TestUpiLateCapture:
    def test_scenario_shape(self):
        from carbon.compiler import compile_scenario
        from carbon.scenarios import load_scenario

        _, scenario = load_scenario("upi-timeout")
        plan = compile_scenario(scenario, overrides={"baseline_orders": 10})
        late = next(s for s in plan.steps if s.action_type == "late_events")
        assert late.params == {"for_each": "failed_payments", "state": "captured"}
        assert scenario.verify["expect"] == "unchanged"

    def test_buggy_handler_leaves_cancelled_but_paid(self, shop):
        s, url = shop(bugs={"ignore_cancelled"})
        result = _carbon(
            "upi-timeout", "--set", "baseline_orders=10", "--set", "late_delay_s=0.1",
            "--webhook-url", url, "--verify-cmd", s.count_cmd(CANCELLED_BUT_PAID), "--ci",
        )
        assert result.exit_code == 1, _text(result)
        out = _text(result)
        assert "checker: 0 -> 3" in out
        assert "refund the payment or reinstate the order" in out

    def test_fixed_handler_reinstates(self, shop):
        s, url = shop()
        result = _carbon(
            "upi-timeout", "--set", "baseline_orders=10", "--set", "late_delay_s=0.1",
            "--webhook-url", url, "--verify-cmd", s.count_cmd(CANCELLED_BUT_PAID), "--ci",
        )
        assert result.exit_code == 0, _text(result)

    def test_timed_out_payment_carries_upi_timeout_reason(self, shop):
        received = []
        s, url = shop()
        orig = s.handle
        s.handle = lambda event: (received.append(event), orig(event))
        _carbon("upi-timeout", "--set", "baseline_orders=10", "--set", "late_delay_s=0.1", "--webhook-url", url)
        failed = [e["payload"]["payment"]["entity"] for e in received if e["event"] == "payment.failed"]
        late = [e["payload"]["payment"]["entity"] for e in received if e["event"] == "payment.captured"
                and e["payload"]["payment"]["entity"]["id"] in {f["id"] for f in failed}]
        assert len(failed) == 3 and len(late) == 3
        assert {f["error_reason"] for f in failed} == {"payment_timed_out"}
        assert {f["method"] for f in failed} == {"upi"}
        assert all("error_reason" not in e for e in late)

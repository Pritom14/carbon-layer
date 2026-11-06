"""--fault-cmd / --recover-cmd: the first delivery fails half way, then the gateway retries.

A handler that records the event id as processed and applies the credit in two separate
writes (two databases, or two transactions) skips every retry once the first attempt failed
between them: the customer paid and the wallet never moved. The end-to-end tests run the real
CLI against a tiny wallet handler in three shapes and break its credit write with a flag file
(standing in for "revoke INSERT on the credits table" or "stop the wallet database").
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shlex
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

from carbon.cli import app
from carbon.webhook.verify import evaluate

try:  # click < 8.2 mixes stderr into stdout unless told otherwise
    runner = CliRunner(mix_stderr=False)
except TypeError:
    runner = CliRunner()

SECRET = "carbon"
ORDERS = "4"


def _text(result) -> str:
    return " ".join((result.stdout + result.stderr).split())


@pytest.fixture(autouse=True)
def _sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/carbon.db")
    for var in ("RAZORPAY_API_KEY", "RAZORPAY_API_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


class Wallet:
    """shape: 'split' (event ids in events.db, credits in wallet.db, two commits), 'atomic'
    (both in wallet.db, one transaction) or 'swallow' (one transaction, but a failed write is
    answered with 200). The credit write fails while the file ``broken`` exists."""

    def __init__(self, root: Path, shape: str):
        self.root, self.shape = root, shape
        self.wallet = root / "wallet.db"
        self.events = root / ("events.db" if shape == "split" else "wallet.db")
        self.flag = root / "broken"
        for path in {self.wallet, self.events}:
            db = sqlite3.connect(path)
            db.executescript(
                "create table if not exists processed(event_id text primary key);"
                "create table if not exists credits(payment_id text primary key, amount integer);"
            )
            db.close()

    def credit(self, db, payment_id, amount):
        if self.flag.exists():
            raise sqlite3.OperationalError("wallet database unavailable")
        db.execute("insert into credits values (?, ?)", (payment_id, amount))

    def handle(self, event_id, entity) -> int:
        if self.shape == "split":
            events = sqlite3.connect(self.events)
            try:
                with events:  # claim first, in its own database
                    events.execute("insert into processed values (?)", (event_id,))
            except sqlite3.IntegrityError:
                return 200  # already processed
            finally:
                events.close()
            wallet = sqlite3.connect(self.wallet)
            try:
                with wallet:
                    self.credit(wallet, entity["id"], entity["amount"])
            except sqlite3.OperationalError:
                return 500
            finally:
                wallet.close()
            return 200
        db = sqlite3.connect(self.wallet)
        try:
            with db:  # claim and credit commit together or not at all
                db.execute("insert into processed values (?)", (event_id,))
                self.credit(db, entity["id"], entity["amount"])
        except sqlite3.IntegrityError:
            return 200
        except sqlite3.OperationalError:
            return 200 if self.shape == "swallow" else 500
        finally:
            db.close()
        return 200

    def serve(self):
        wallet = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(sig, self.headers.get("X-Razorpay-Signature", "")):
                    return self._reply(401)
                event = json.loads(raw)
                code = 200
                if event["event"] == "payment.captured":
                    entity = event["payload"]["payment"]["entity"]
                    code = wallet.handle(self.headers.get("X-Razorpay-Event-Id"), entity)
                self._reply(code)

            def _reply(self, code):
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.server = server
        return f"http://127.0.0.1:{server.server_address[1]}/webhooks"

    def count(self, sql="select count(*) from credits", path=None):
        return sqlite3.connect(path or self.wallet).execute(sql).fetchone()[0]

    def count_cmd(self):
        code = f"import sqlite3; print(sqlite3.connect({str(self.wallet)!r}).execute('select count(*) from credits').fetchone()[0])"
        return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"

    def fault(self):
        return ["--fault-cmd", f"touch {shlex.quote(str(self.flag))}", "--recover-cmd", f"rm -f {shlex.quote(str(self.flag))}"]


@pytest.fixture
def wallet(tmp_path):
    started = []

    def make(shape):
        root = tmp_path / f"{shape}{len(started)}"
        root.mkdir()
        w = Wallet(root, shape)
        url = w.serve()
        started.append(w)
        return w, url

    yield make
    for w in started:
        w.server.shutdown()
        w.server.server_close()


def _carbon(w, url, *args):
    return runner.invoke(
        app,
        [
            "run", "flash-sale", "-p", "razorpay", "--webhook-secret", SECRET, "--set", f"baseline_orders={ORDERS}",
            "--webhook-url", url, "--verify-cmd", w.count_cmd(), *args,
        ],
    )


def test_claim_and_credit_in_two_databases_loses_the_payment(wallet):
    w, url = wallet("split")
    result = _carbon(w, url, *w.fault(), "--ci")
    out = _text(result)
    assert result.exit_code == 1, out
    paid = w.count("select count(*) from processed", w.events)
    assert paid > 0 and w.count() == 0  # every event marked processed, nothing credited
    assert f"{paid} unique events acknowledged (2xx) but only 0 side effects after a failed first delivery" in out
    assert "marked the event processed before the write failed" in out
    assert "one database transaction" in out and "reconcile missing effects by payment id" in out
    assert f"Fault mode: {paid} first deliveries with the write broken (0 returned 2xx), {paid} retries" in out
    assert not w.flag.exists()  # recovered


def test_claim_and_credit_in_one_transaction_passes(wallet):
    w, url = wallet("atomic")
    result = _carbon(w, url, *w.fault(), "--ci")
    out = _text(result)
    assert result.exit_code == 0, out  # the failed first deliveries are not graded as 5xx failures
    assert w.count() == w.count("select count(*) from processed") > 0
    assert "each event processed exactly once" in out
    assert "with the fault:" in out and "returned 2xx), counter 0 -> 0" in out


def test_a_swallowed_error_is_never_retried(wallet):
    w, url = wallet("swallow")
    result = _carbon(w, url, *w.fault(), "--ci", "--output", "json")
    assert result.exit_code == 1, _text(result)
    data = json.loads(result.stdout)
    v = data["verification"]
    assert v["verdict"] == "lost" and w.count() == 0
    assert v["fault"]["first_deliveries_acked"] == v["unique_events_acked"] > 0
    assert "swallowed the error, so a gateway would never retry them" in v["message"]
    assert data["webhooks"]["during_fault"] == data["webhooks"]["total"]  # nothing to retry


def test_a_fault_that_breaks_nothing_fails_the_run(wallet):
    w, url = wallet("split")
    result = _carbon(w, url, "--fault-cmd", "true", "--recover-cmd", "true", "--ci")
    out = _text(result)
    assert result.exit_code == 1, out
    assert "FAIL: the fault had no effect" in out and "the test proves nothing" in out


def test_recover_runs_even_when_the_fault_cmd_fails(wallet):
    w, url = wallet("atomic")
    flag = shlex.quote(str(w.flag))
    result = _carbon(w, url, "--fault-cmd", f"touch {flag}; exit 3", "--recover-cmd", f"rm -f {flag}")
    assert result.exit_code == 2, _text(result)
    assert "--fault-cmd exited 3" in _text(result)
    assert not w.flag.exists()


@pytest.mark.parametrize(
    "args, error",
    [
        (["--fault-cmd", "true"], "--fault-cmd and --recover-cmd go together"),
        (["--recover-cmd", "true"], "--fault-cmd and --recover-cmd go together"),
        (["--fault-cmd", "true", "--recover-cmd", "true", "--verify-expect", "unchanged"], "--fault-cmd needs --verify-cmd"),
        (["--fault-cmd", "true", "--recover-cmd", "true", "--webhook-signature", "missing"], "needs --webhook-signature valid"),
    ],
)
def test_fault_flags_are_checked(wallet, args, error):
    w, url = wallet("atomic")
    result = _carbon(w, url, *args)
    assert result.exit_code == 2 and error in _text(result)


def test_fault_needs_a_verify_cmd():
    result = runner.invoke(
        app,
        ["run", "flash-sale", "-p", "razorpay", "--webhook-url", "http://127.0.0.1:9/x", "--fault-cmd", "true", "--recover-cmd", "true"],
    )
    assert result.exit_code == 2 and "--fault-cmd needs --verify-cmd" in _text(result)


def _d(eid, ok=True, fault=False):
    return {"event_type": "payment.captured", "ok": ok, "provider_event_id": eid, "fault": fault}


def test_evaluate_reports_what_happened_during_the_fault():
    deliveries = [_d("a", ok=False, fault=True), _d("b", ok=False, fault=True), _d("a"), _d("b")]
    v = evaluate(10, 12, deliveries, "payment.captured", after_fault=10)
    assert v["passed"] and v["fault"] == {
        "first_deliveries": 2,
        "first_deliveries_acked": 0,
        "counter_after_fault": 10,
        "change_during_fault": 0,
    }
    partial = evaluate(10, 12, deliveries, "payment.captured", after_fault=11)
    assert partial["passed"] and "did not block every write" in partial["message"]
    useless = evaluate(10, 12, [_d("a", fault=True), _d("b", fault=True)], "payment.captured", after_fault=12)
    assert useless["verdict"] == "fault_ineffective" and not useless["passed"]


def test_evaluate_without_a_fault_is_unchanged():
    v = evaluate(0, 1, [_d("a"), _d("b")], "payment.captured")
    assert v["verdict"] == "lost" and "fault" not in v and "acked without being processed" in v["message"]

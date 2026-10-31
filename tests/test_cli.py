"""CLI behaviour: exit codes, JSON output, signature-acceptance failures, hints."""

from __future__ import annotations

import json
import socket

import httpx
import pytest
import respx
from typer.testing import CliRunner

from carbon import __version__
from carbon.cli import app
from carbon.webhook.checks import delivery_hints, evaluate_deliveries, overall_passed
from carbon.webhook.sender import _apply_signature_mode, _sign_cashfree

try:  # click < 8.2 mixes stderr into stdout unless told otherwise
    runner = CliRunner(mix_stderr=False)
except TypeError:
    runner = CliRunner()
URL = "http://handler.test/webhooks"


def _text(result) -> str:
    """Everything the command printed, stdout and stderr."""
    return result.stdout + result.stderr


@pytest.fixture(autouse=True)
def _sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/carbon.db")
    for var in ("RAZORPAY_API_KEY", "RAZORPAY_API_SECRET", "JUSPAY_API_KEY", "JUSPAY_MERCHANT_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)  # no stray .env


def _run(*args: str):
    return runner.invoke(app, ["run", "min-amount", "-p", "razorpay", *args])


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


@pytest.mark.parametrize(
    "flag,value,choices",
    [
        ("--provider", "paypall", "razorpay"),
        ("--webhook-order", "bogus", "sequence"),
        ("--webhook-signature", "bogus", "wrong_secret"),
        ("--output", "xml", "json"),
    ],
)
def test_invalid_enum_flags_exit_2(flag, value, choices):
    result = runner.invoke(app, ["run", "min-amount", flag, value])
    assert result.exit_code == 2
    assert choices in _text(result)


def test_json_output_is_pure_json():
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200))
        result = _run("--webhook-url", URL, "--output", "json")
    assert result.exit_code == 0, _text(result)
    data = json.loads(result.stdout)
    assert data["provider"] == "razorpay"
    assert data["webhook_format"] == "razorpay"
    assert data["adapter"] == "simulated"
    assert data["carbon_version"] == __version__
    assert data["passed"] is True
    assert "Simulating payments locally" in result.stderr


def test_no_keys_warning_or_waitlist_in_output():
    result = runner.invoke(app, ["run", "dispute-spike", "-p", "razorpay", "--set", "baseline_orders=10"])
    assert result.exit_code == 0, _text(result)
    assert "keys not set" not in _text(result)
    assert "waitlist" not in _text(result).lower()
    assert "Nothing tested: add --webhook-url" in _text(result)
    assert "✓" not in _text(result)
    assert "Webhook format: razorpay" in _text(result)
    listing = runner.invoke(app, ["scenarios-list"])
    assert "waitlist" not in _text(listing).lower()
    assert "locked" not in _text(listing)


@pytest.mark.parametrize("mode", ["missing", "corrupted", "wrong_secret"])
def test_ci_fails_when_bad_signatures_are_accepted(mode):
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200))
        result = _run("--webhook-url", URL, "--webhook-signature", mode, "--ci")
    assert result.exit_code == 1
    assert "handler accepted 1" in _text(result)
    assert "CI check failed" in _text(result)


def test_bad_signature_failure_in_json():
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200))
        result = _run("--webhook-url", URL, "--webhook-signature", "missing", "--output", "json")
    data = json.loads(result.stdout)
    assert data["passed"] is False
    check = next(f for f in data["findings"] if f["check"] == "rejects_bad_signatures")
    assert check["passed"] is False
    assert "accepted 1 unsigned webhooks" in check["message"]


@pytest.mark.parametrize("status", [400, 401, 403])
def test_ci_passes_when_bad_signatures_are_rejected(status):
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(status))
        result = _run("--webhook-url", URL, "--webhook-signature", "missing", "--ci", "--output", "json")
    assert result.exit_code == 0, _text(result)
    data = json.loads(result.stdout)
    check = next(f for f in data["findings"] if f["check"] == "rejects_bad_signatures")
    assert check["passed"] and "rejected 1 of 1 unsigned webhooks" in check["message"]


def test_ci_still_fails_on_5xx():
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(500))
        result = _run("--webhook-url", URL, "--ci")
    assert result.exit_code == 1


def test_secret_mismatch_hint():
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(401))
        result = _run("--webhook-url", URL)
    assert _text(result).count("secret probably differs") == 1


def test_connection_refused_hint_and_exit_3():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    result = _run("--webhook-url", f"http://127.0.0.1:{port}/webhooks", "--ci")
    assert result.exit_code == 3
    assert "Nothing is listening" in _text(result)


def test_min_amount_captures_at_order_amount():
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(200))
        result = _run("--webhook-url", URL)
    assert result.exit_code == 0, _text(result)
    payload = json.loads(route.calls[0].request.content)
    assert payload["event"] == "payment.captured"
    assert payload["payload"]["payment"]["entity"]["amount"] == 100


def test_real_razorpay_keys_simulate_instead_of_crashing(monkeypatch):
    monkeypatch.setenv("RAZORPAY_API_KEY", "rzp_test_x")
    monkeypatch.setenv("RAZORPAY_API_SECRET", "secret")
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(200))
        result = runner.invoke(
            app,
            ["run", "dispute-spike", "-p", "razorpay", "--set", "baseline_orders=10", "--webhook-url", URL],
        )
    assert result.exit_code == 0, _text(result)
    assert route.call_count > 0
    assert _text(result).count("keys ignored") == 1


def test_report_accepts_run_id_option():
    result = _run("--output", "json")
    run_id = json.loads(result.stdout)["run_id"]
    for args in (["report", run_id], ["report", "--run-id", run_id]):
        out = runner.invoke(app, args)
        assert out.exit_code == 0
        assert "min-amount" in _text(out)
    assert runner.invoke(app, ["report"]).exit_code == 2


def test_cashfree_corrupted_mode_only_touches_signature():
    headers = _sign_cashfree(b"{}", "s")
    ts, version = headers["x-webhook-timestamp"], headers["x-webhook-version"]
    corrupted = _apply_signature_mode(dict(headers), "corrupted")
    assert corrupted["x-webhook-signature"].endswith("CORRUPTED")
    assert corrupted["x-webhook-timestamp"] == ts and ts.isdigit()
    assert corrupted["x-webhook-version"] == version
    missing = _apply_signature_mode(dict(headers), "missing")
    assert "x-webhook-signature" not in missing
    assert missing["x-webhook-timestamp"] == ts


def test_juspay_missing_mode_drops_authorization():
    headers = {"Content-Type": "application/json", "Authorization": "Basic abc"}
    assert "Authorization" not in _apply_signature_mode(dict(headers), "missing")


def _d(status, error=None):
    return {"status_code": status, "error": error}


def test_evaluate_deliveries_bad_signature_accepted():
    findings = evaluate_deliveries([_d(200), _d(401)], "corrupted")
    check = findings[0]
    assert check["check"] == "rejects_bad_signatures" and not check["passed"]
    assert "accepted 1 corruptly signed" in check["message"]
    assert not overall_passed(findings)


def test_evaluate_deliveries_valid_mode_has_no_signature_check():
    findings = evaluate_deliveries([_d(200)], "valid")
    assert [f["check"] for f in findings] == ["webhook_delivery"]
    assert overall_passed(findings)


def test_informational_findings_never_fail_run():
    assert overall_passed([{"severity": "info", "passed": False}])
    assert not overall_passed([], {"passed": False})


def test_hints():
    assert delivery_hints([_d(401)] * 10, "valid", URL)
    assert not delivery_hints([_d(401)] * 10, "missing", URL)
    assert not delivery_hints([_d(200)] * 9 + [_d(401)], "valid", URL)
    refused = delivery_hints([_d(None, "ConnectError: refused")], "valid", URL)
    assert len(refused) == 1 and "Nothing is listening" in refused[0]

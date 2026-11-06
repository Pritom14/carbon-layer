"""Side-effect verification: did the handler process each event exactly once?

HTTP status codes can't reveal double-processing: a handler that credits a wallet
twice still returns 200. The user supplies a command that prints a number reflecting
the side effect (e.g. a SQL count of wallet credits); we compare its change across
the webhook delivery with the number of unique events acknowledged.
"""

from __future__ import annotations

import subprocess


class VerifyError(RuntimeError):
    pass


def read_counter(cmd: str, timeout_s: float = 60.0) -> float:
    """Run a shell command and parse its last non-empty output line as a number."""
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        raise VerifyError(f"--verify-cmd timed out after {timeout_s:.0f}s") from e
    if proc.returncode != 0:
        raise VerifyError(f"--verify-cmd exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        raise VerifyError("--verify-cmd printed nothing; it must print a number")
    try:
        return float(lines[-1])
    except ValueError as e:
        raise VerifyError(f"--verify-cmd must print a number, got: {lines[-1][:80]!r}") from e


def run_hook(cmd: str, flag: str, timeout_s: float = 120.0) -> None:
    """Run a --fault-cmd / --recover-cmd shell command; any failure stops the run."""
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        raise VerifyError(f"{flag} timed out after {timeout_s:.0f}s") from e
    if proc.returncode != 0:
        raise VerifyError(f"{flag} exited {proc.returncode}: {proc.stderr.strip()[:200]}")


EXPECT_ACKED = "acked"
EXPECT_UNCHANGED = "unchanged"


def evaluate(
    before: float,
    after: float,
    deliveries: list[dict],
    event_type: str,
    client_calls: list[dict] | None = None,
    burst: int = 1,
    after_fault: float | None = None,
) -> dict:
    """Compare observed counter change with unique acknowledged events of event_type.

    With client_calls (client-side verify requests), the unit is the payment: a payment
    acknowledged on either path should cause exactly one side effect.

    after_fault (fault mode): the counter read after --recover-cmd, before the retries. The
    first delivery of every event ran with the write broken, so it should not have moved.
    """
    if client_calls is not None:

        def key(d: dict):
            return d.get("remote_id")

        acked = {key(d) for d in deliveries if d.get("event_type") == event_type and d.get("ok")}
        acked |= {c["payment_id"] for c in client_calls if c.get("ok")}
        acked.discard(None)
        acked.discard("")
    else:

        def key(d: dict):
            return d.get("provider_event_id") or d.get("payload")

        acked = {key(d) for d in deliveries if d.get("event_type") == event_type and d.get("ok")}
    sent = sum(1 for d in deliveries if d.get("event_type") == event_type)
    expected = len(acked)
    observed = after - before
    unit = "payments" if client_calls is not None else "events"
    fault = None
    if after_fault is not None:
        first = [d for d in deliveries if d.get("fault") and d.get("event_type") == event_type]
        swallowed = len({key(d) for d in first if d.get("ok")})
        fault = {
            "first_deliveries": len(first),
            "first_deliveries_acked": swallowed,
            "counter_after_fault": after_fault,
            "change_during_fault": after_fault - before,
        }
    if fault and expected and fault["change_during_fault"] >= expected:
        verdict = "fault_ineffective"
        message = (
            f"{event_type}: --fault-cmd did not stop the side effect. The counter rose by "
            f"{fault['change_during_fault']:g} while the fault was active, once for each of the "
            f"{expected} {unit}, so no retry was ever needed and the test proves nothing. Make "
            "--fault-cmd break the write your handler makes (revoke INSERT on the credits table, "
            "stop the database that holds them)"
        )
    elif observed == expected:
        verdict = "pass"
        message = (
            f"{event_type}: each payment processed exactly once across webhook and client verify"
            if client_calls is not None
            else f"{event_type}: each event processed exactly once"
        )
    elif observed > expected:
        verdict = "duplicates"
        message = f"{event_type}: {expected} unique {unit} caused {observed:g} side effects; handler is not idempotent"
        if client_calls is not None:
            message += " (the webhook and the client verify call both apply the effect; key it on the payment id)"
        elif burst > 1:
            message += (
                f" ({burst} copies arrived at the same moment and each passed an 'already processed?' "
                "check before any was saved: a check-then-insert race. Put a unique constraint on "
                "the event or payment id and insert with ON CONFLICT DO NOTHING)"
            )

    elif fault:
        verdict = "lost"
        message = (
            f"{event_type}: {expected} unique {unit} acknowledged (2xx) but only {observed:g} side "
            "effects after a failed first delivery."
        )
        retried = expected - fault["first_deliveries_acked"]
        if retried > 0:
            message += (
                " The first delivery ran with the write broken and the handler marked the event "
                "processed before the write failed, so every retry was skipped. Record the event "
                "id and apply the effect in one database transaction, or reconcile missing effects "
                "by payment id"
            )
        if fault["first_deliveries_acked"]:
            message += (
                f"{'.' if retried > 0 else ''} {fault['first_deliveries_acked']} first deliveries "
                "returned 2xx while the write was broken: the handler swallowed the error, so a "
                "gateway would never retry them. Return 5xx when the effect was not applied"
            )
    else:
        verdict = "lost"
        message = (
            f"{event_type}: {expected} unique {unit} acknowledged (2xx) but only "
            f"{observed:g} side effects; events were acked without being processed"
        )
    if fault and verdict == "pass" and fault["change_during_fault"] > 0:
        message += (
            f" (note: the counter rose by {fault['change_during_fault']:g} while the fault was "
            "active, so --fault-cmd did not block every write)"
        )
    result = {
        "mode": EXPECT_ACKED,
        "event_type": event_type,
        "deliveries": sent,
        "unique_events_acked": expected,
        "client_verify_calls": len(client_calls) if client_calls is not None else None,
        "before": before,
        "after": after,
        "observed_change": observed,
        "verdict": verdict,
        "passed": verdict == "pass",
        "message": message,
    }
    if fault:
        result["fault"] = fault
    return result


def evaluate_unchanged(
    before: float,
    after: float,
    deliveries: list[dict],
    counts: str = "records in a bad state",
    fix: str | None = None,
) -> dict:
    """The command counts records in a bad state (an order paid twice, a cancelled order that
    was paid); delivering the scenario's webhooks must not add any."""
    change = after - before
    passed = change <= 0
    message = f"no new {counts}" if passed else f"{change:g} new {counts}"
    return {
        "mode": EXPECT_UNCHANGED,
        "event_type": None,
        "deliveries": len(deliveries),
        "unique_events_acked": len({d.get("provider_event_id") for d in deliveries if d.get("ok")} - {None}),
        "client_verify_calls": None,
        "before": before,
        "after": after,
        "observed_change": change,
        "counts": counts,
        "fix": None if passed else fix,
        "verdict": "pass" if passed else "bad_state",
        "passed": passed,
        "message": message,
    }

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


def evaluate(before: float, after: float, deliveries: list[dict], event_type: str) -> dict:
    """Compare observed counter change with unique acknowledged events of event_type."""
    acked = {
        d.get("provider_event_id") or d.get("payload")
        for d in deliveries
        if d.get("event_type") == event_type and d.get("ok")
    }
    sent = sum(1 for d in deliveries if d.get("event_type") == event_type)
    expected = len(acked)
    observed = after - before
    if observed == expected:
        verdict, message = "pass", f"{event_type}: each event processed exactly once"
    elif observed > expected:
        verdict = "duplicates"
        message = (
            f"{event_type}: {expected} unique events caused {observed:g} side effects; "
            f"handler is not idempotent"
        )
    else:
        verdict = "lost"
        message = (
            f"{event_type}: {expected} unique events acknowledged (2xx) but only "
            f"{observed:g} side effects; events were acked without being processed"
        )
    return {
        "event_type": event_type,
        "deliveries": sent,
        "unique_events_acked": expected,
        "before": before,
        "after": after,
        "observed_change": observed,
        "verdict": verdict,
        "passed": verdict == "pass",
        "message": message,
    }

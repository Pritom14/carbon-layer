"""Pass/fail checks graded against the handler's responses (not against the simulator)."""

from __future__ import annotations

from typing import Optional

# Expected rejection codes for an unsigned or badly signed webhook
REJECTION_CODES = (400, 401, 403)

_MODE_LABEL = {
    "missing": "unsigned",
    "corrupted": "corruptly signed",
    "wrong_secret": "wrongly signed",
}


def _is_connect_error(d: dict) -> bool:
    return d.get("status_code") is None and str(d.get("error") or "").startswith("ConnectError")


def _is_5xx_or_timeout(d: dict) -> bool:
    sc = d.get("status_code")
    return sc is None or (isinstance(sc, int) and sc >= 500)


def _is_2xx(d: dict) -> bool:
    sc = d.get("status_code")
    return isinstance(sc, int) and 200 <= sc < 300


def nothing_listening(deliveries: list[dict]) -> bool:
    """True when every delivery failed to connect (nothing is listening on the URL)."""
    return bool(deliveries) and all(_is_connect_error(d) for d in deliveries)


def evaluate_deliveries(deliveries: list[dict], signature_mode: str = "valid") -> list[dict]:
    """
    Grade webhook deliveries. Returns findings as dicts with keys
    check, severity, passed, message, details.
    """
    if not deliveries:
        return []
    total = len(deliveries)
    findings: list[dict] = []

    if signature_mode != "valid":
        label = _MODE_LABEL.get(signature_mode, "invalid")
        accepted = [d for d in deliveries if _is_2xx(d)]
        rejected = sum(
            1 for d in deliveries if isinstance(d.get("status_code"), int) and d["status_code"] in REJECTION_CODES
        )
        if accepted:
            message = f"handler accepted {len(accepted)} {label} webhooks (expected 400/401/403)"
        else:
            message = f"handler rejected {rejected} of {total} {label} webhooks with 400/401/403"
        findings.append({
            "check": "rejects_bad_signatures",
            "severity": "critical",
            "passed": not accepted,
            "message": message,
            "details": {"signature_mode": signature_mode, "expected": "400/401/403", "got": f"{len(accepted)} x 2xx"},
        })

    errors = [d for d in deliveries if _is_5xx_or_timeout(d)]
    findings.append({
        "check": "webhook_delivery",
        "severity": "high",
        "passed": not errors,
        "message": (
            f"{len(errors)} of {total} webhooks returned 5xx or got no response (timeout/connection error)"
            if errors
            else f"no 5xx or missing responses across {total} webhooks"
        ),
        "details": {"expected": "no 5xx/timeout", "got": f"{len(errors)} of {total}"},
    })
    return findings


def delivery_hints(deliveries: list[dict], signature_mode: str, target_url: str) -> list[str]:
    """Plain-language hints for common setup mistakes."""
    if not deliveries:
        return []
    hints: list[str] = []
    if nothing_listening(deliveries):
        hints.append(
            f"Nothing is listening on {target_url} (connection refused). "
            "Start your handler, or check the host and port."
        )
        return hints
    if signature_mode == "valid":
        auth_rejected = sum(1 for d in deliveries if d.get("status_code") in (401, 403))
        if auth_rejected / len(deliveries) > 0.9:
            hints.append(
                f"Your handler rejected {auth_rejected} of {len(deliveries)} validly signed webhooks "
                "with 401/403. Your secret probably differs: pass --webhook-secret "
                "(the default is 'carbon')."
            )
    return hints


def overall_passed(findings: list[dict], verification: Optional[dict] = None) -> bool:
    """A run passes when every non-informational finding passed and verification (if any) passed."""
    graded = [f for f in findings if f.get("severity") != "info"]
    return all(f["passed"] for f in graded) and (verification is None or verification["passed"])

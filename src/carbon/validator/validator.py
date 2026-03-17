"""Run validations for a run and store findings."""

from __future__ import annotations

import re
from typing import Any

from carbon.scenarios.models import Scenario
from carbon.scenarios.registry import load_scenario
from carbon.storage.repo import get_entity_map, get_run, insert_finding

# Metric names that scenarios can reference in validations (check) and findings (condition).
METRIC_NAMES = frozenset({
    "orders_count",
    "payments_captured_count",
    "payments_attempted_count",
    "payment_success_rate",
    "disputes_count",
    "refunds_count",
})


def _compute_metrics(entity_map: dict) -> dict[str, Any]:
    """Compute run metrics from entity_map for validation/findings."""
    orders = [k for k, v in entity_map.items() if v.get("entity_type") == "order"]
    payments = [k for k, v in entity_map.items() if v.get("entity_type") == "payment"]
    captured = [
        k for k, v in entity_map.items()
        if v.get("entity_type") == "payment" and (v.get("state") or "").lower() == "captured"
    ]
    disputes = [k for k, v in entity_map.items() if v.get("entity_type") == "dispute"]
    refunds = [k for k, v in entity_map.items() if v.get("entity_type") == "refund"]
    attempted = len(payments)
    return {
        "orders_count": len(orders),
        "payments_captured_count": len(captured),
        "payments_attempted_count": attempted,
        "payment_success_rate": len(captured) / attempted if attempted else 0.0,
        "disputes_count": len(disputes),
        "refunds_count": len(refunds),
    }


def _evaluate_expected(actual: Any, expected: Any) -> tuple[bool, str]:
    """
    Compare actual to expected. expected can be:
    - number (int/float): pass if actual == expected (with float tolerance for rates).
    - bool: pass if bool(actual) == expected.
    - str: predicate like ">= 1000", "== 150", "> 0.7", "<= 0.3".
    Returns (passed, message).
    """
    if expected is True or expected is False:
        ok = bool(actual) == expected
        return ok, f"expected {expected}, got {actual!r}"
    if isinstance(expected, (int, float)):
        if isinstance(actual, float) or isinstance(expected, float):
            ok = abs(float(actual) - float(expected)) < 1e-9
        else:
            ok = actual == expected
        return ok, f"expected {expected}, got {actual}"
    if isinstance(expected, str):
        s = expected.strip()
        m = re.match(r"(>=|<=|==|!=|>|<)\s*(.+)", s)
        if m:
            op, val_str = m.group(1), m.group(2).strip()
            try:
                val = int(val_str) if "." not in val_str else float(val_str)
            except ValueError:
                return False, f"invalid expected value: {val_str!r}"
            try:
                actual_num = float(actual) if isinstance(actual, (int, float)) else float(actual)
            except (TypeError, ValueError):
                actual_num = actual
            if op == ">=":
                ok = actual_num >= val
            elif op == "<=":
                ok = actual_num <= val
            elif op == "==":
                ok = abs(actual_num - val) < 1e-9 if isinstance(val, float) else actual_num == val
            elif op == "!=":
                ok = actual_num != val
            elif op == ">":
                ok = actual_num > val
            else:
                ok = actual_num < val
            return ok, f"expected {s}, got {actual}"
    return False, f"expected {expected!r}, got {actual!r}"


def _evaluate_condition(condition: str, metrics: dict[str, Any]) -> bool:
    """Evaluate condition string against metrics. Format: metric_name op value."""
    if not condition or not isinstance(condition, str):
        return False
    condition = condition.strip()
    for name in METRIC_NAMES:
        if name in metrics and condition.startswith(name):
            rest = condition[len(name):].strip()
            m = re.match(r"(>=|<=|==|!=|>|<)\s*(.+)", rest)
            if m:
                op, val_str = m.group(1), m.group(2).strip()
                try:
                    val = int(val_str) if "." not in val_str else float(val_str)
                except ValueError:
                    return False
                actual = metrics[name]
                try:
                    actual_num = float(actual)
                except (TypeError, ValueError):
                    return False
                if op == ">=":
                    return actual_num >= val
                if op == "<=":
                    return actual_num <= val
                if op == "==":
                    return abs(actual_num - val) < 1e-9 if isinstance(val, float) else actual_num == val
                if op == "!=":
                    return actual_num != val
                if op == ">":
                    return actual_num > val
                if op == "<":
                    return actual_num < val
            break
    return False


async def run_validations(run_id: str) -> None:
    """Load run state, load scenario if present, run scenario validations/findings and/or default checks."""
    run = await get_run(run_id)
    if not run:
        return
    entity_map = await get_entity_map(run_id)
    metrics = _compute_metrics(entity_map)

    scenario: Scenario | None = None
    try:
        _, scenario = load_scenario(run["scenario_name"])
    except LookupError:
        pass

    if scenario and (scenario.validations or scenario.findings):
        for v in scenario.validations:
            check = (v.check or "").strip()
            if check not in metrics:
                await insert_finding(
                    run_id,
                    check or "unknown",
                    v.severity or "high",
                    False,
                    f"Unknown check: {check!r}",
                    {"expected": v.expected},
                )
                continue
            actual = metrics[check]
            passed, message = _evaluate_expected(actual, v.expected)
            await insert_finding(
                run_id,
                check,
                v.severity or "high",
                passed,
                message,
                {"expected": v.expected, "got": actual, "description": v.description},
            )
        for f in scenario.findings:
            if not isinstance(f, dict):
                continue
            condition = f.get("condition")
            if not condition:
                continue
            if _evaluate_condition(str(condition), metrics):
                message = f.get("message") or condition
                severity = f.get("severity") or "medium"
                await insert_finding(
                    run_id,
                    "finding",
                    severity,
                    False,
                    message,
                    {"condition": condition},
                )
        await _insert_default_findings(run_id, entity_map, severity="low")
    else:
        await _insert_default_findings(run_id, entity_map, severity_orders="high", severity_rest="medium")


async def _insert_default_findings(
    run_id: str,
    entity_map: dict,
    severity: str | None = None,
    severity_orders: str | None = None,
    severity_rest: str | None = None,
) -> None:
    """Insert the four default count findings."""
    sev_ord = severity or severity_orders or "high"
    sev_rest = severity or severity_rest or "medium"
    orders = [k for k, v in entity_map.items() if v.get("entity_type") == "order"]
    captured = [
        k for k, v in entity_map.items()
        if v.get("entity_type") == "payment" and (v.get("state") or "").lower() == "captured"
    ]
    disputes = [k for k, v in entity_map.items() if v.get("entity_type") == "dispute"]
    refunds = [k for k, v in entity_map.items() if v.get("entity_type") == "refund"]
    await insert_finding(
        run_id, "orders_created", sev_ord, len(orders) > 0,
        f"Orders created: {len(orders)}", {"count": len(orders)},
    )
    await insert_finding(
        run_id, "payments_captured", sev_ord, len(captured) > 0,
        f"Payments captured: {len(captured)}", {"count": len(captured)},
    )
    await insert_finding(
        run_id, "disputes_created", sev_rest, True,
        f"Disputes created: {len(disputes)}", {"count": len(disputes)},
    )
    await insert_finding(
        run_id, "refunds_created", sev_rest, True,
        f"Refunds created: {len(refunds)}", {"count": len(refunds)},
    )

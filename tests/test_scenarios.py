"""Integration tests: run each scenario with mock adapter and assert outcomes (requires PostgreSQL)."""

from __future__ import annotations

import pytest

from carbon.adapters import get_adapter
from carbon.compiler import compile_scenario
from carbon.engine import run_plan
from carbon.scenarios import load_scenario
from carbon.storage.repo import create_run, get_entity_map, get_findings, get_run
from carbon.validator import run_validations


# Expected entity counts per scenario (min bounds; actual may vary slightly for probabilistic steps).
SCENARIO_EXPECTATIONS = {
    "dispute-spike": {
        "orders_count": 1000,
        "payments_captured_count": 1000,
        "disputes_count": 150,
    },
    "payment-decline-spike": {
        "orders_count": 2000,
        "payments_captured_count_min": 1200,  # ~70% of 2000
        "disputes_count": 0,
    },
    "refund-storm": {
        "orders_count": 2000,
        "payments_captured_count": 2000,
        "refunds_count": 500,
    },
    "gateway-error-burst": {
        "orders_count": 1000,
        "payments_captured_count_min": 400,  # ~50% of 1000
    },
    "flash-sale": {
        "orders_count": 5000,
        "payments_captured_count": 5000,
    },
    "min-amount": {
        "orders_count": 1,
        "payments_captured_count": 1,
    },
    "max-amount": {
        "orders_count": 1,
        "payments_captured_count": 1,
    },
}


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_name", [
    "dispute-spike",
    "payment-decline-spike",
    "refund-storm",
    "gateway-error-burst",
    "flash-sale",
    "min-amount",
    "max-amount",
])
async def test_scenario_run_completes(ensure_db, scenario_name):
    """Run scenario end-to-end and assert run completes with expected entity counts."""
    from carbon.storage.repo import ensure_db as repo_ensure_db
    await repo_ensure_db()

    _, scenario = load_scenario(scenario_name)
    plan = compile_scenario(scenario)
    adapter = get_adapter("mock")
    run_id = await create_run(scenario_name, "mock", plan.parameters)
    await run_plan(plan, run_id, "mock", adapter)
    await run_validations(run_id)

    run = await get_run(run_id)
    assert run is not None
    assert run["status"] == "completed"
    assert run["scenario_name"] == scenario_name

    entity_map = await get_entity_map(run_id)
    expectations = SCENARIO_EXPECTATIONS.get(scenario_name, {})
    orders = [k for k, v in entity_map.items() if v.get("entity_type") == "order"]
    captured = [
        k for k, v in entity_map.items()
        if v.get("entity_type") == "payment" and (v.get("state") or "").lower() == "captured"
    ]
    disputes = [k for k, v in entity_map.items() if v.get("entity_type") == "dispute"]
    refunds = [k for k, v in entity_map.items() if v.get("entity_type") == "refund"]

    if "orders_count" in expectations:
        assert len(orders) == expectations["orders_count"], (
            f"orders_count: expected {expectations['orders_count']}, got {len(orders)}"
        )
    if "payments_captured_count" in expectations:
        assert len(captured) == expectations["payments_captured_count"], (
            f"payments_captured_count: expected {expectations['payments_captured_count']}, got {len(captured)}"
        )
    if "payments_captured_count_min" in expectations:
        assert len(captured) >= expectations["payments_captured_count_min"], (
            f"payments_captured_count: expected >={expectations['payments_captured_count_min']}, got {len(captured)}"
        )
    if "disputes_count" in expectations:
        assert len(disputes) == expectations["disputes_count"]
    if "refunds_count" in expectations:
        assert len(refunds) == expectations["refunds_count"]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_name", [
    "dispute-spike",
    "payment-decline-spike",
    "refund-storm",
    "gateway-error-burst",
    "flash-sale",
    "min-amount",
    "max-amount",
])
async def test_scenario_findings_high_severity_pass(ensure_db, scenario_name):
    """After running a scenario, all high/critical severity findings should pass (no failures)."""
    from carbon.storage.repo import ensure_db as repo_ensure_db
    await repo_ensure_db()

    _, scenario = load_scenario(scenario_name)
    plan = compile_scenario(scenario)
    adapter = get_adapter("mock")
    run_id = await create_run(scenario_name, "mock", plan.parameters)
    await run_plan(plan, run_id, "mock", adapter)
    await run_validations(run_id)

    findings = await get_findings(run_id)
    high_critical = [f for f in findings if f.get("severity") in ("high", "critical")]
    failed = [f for f in high_critical if not f.get("passed")]
    assert not failed, (
        f"Scenario {scenario_name}: {len(failed)} high/critical finding(s) failed: "
        + ", ".join(f"{f['check_name']}: {f.get('message', '')}" for f in failed)
    )

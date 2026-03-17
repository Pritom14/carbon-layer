"""Unit tests for compiler and scenario loading (no DB)."""

from __future__ import annotations

import pytest

from carbon.compiler import compile_scenario
from carbon.scenarios.registry import list_scenarios, load_scenario


class TestListScenarios:
    """Test that all expected scenarios are discoverable."""

    def test_list_includes_covered_scenarios(self):
        names = list_scenarios()
        expected = {
            "dispute-spike",
            "payment-decline-spike",
            "refund-storm",
            "gateway-error-burst",
            "flash-sale",
            "min-amount",
            "max-amount",
        }
        for name in expected:
            assert name in names, f"Scenario {name!r} not in list_scenarios()"


class TestCompileScenarios:
    """Test compile_scenario for each covered scenario."""

    @pytest.mark.parametrize("scenario_name", [
        "dispute-spike",
        "payment-decline-spike",
        "refund-storm",
        "gateway-error-burst",
        "flash-sale",
        "min-amount",
        "max-amount",
    ])
    def test_compile_produces_steps(self, scenario_name):
        _, scenario = load_scenario(scenario_name)
        plan = compile_scenario(scenario)
        assert plan.scenario_name == scenario_name
        assert len(plan.steps) >= 1
        step_types = {s.action_type for s in plan.steps}
        assert "create_orders" in step_types

    def test_dispute_spike_has_expected_phases(self):
        _, scenario = load_scenario("dispute-spike")
        plan = compile_scenario(scenario)
        action_types = [s.action_type for s in plan.steps]
        assert "create_orders" in action_types
        assert "create_payments" in action_types
        assert "capture_payments" in action_types
        assert "create_disputes" in action_types

    def test_dispute_spike_has_validations_and_findings(self):
        _, scenario = load_scenario("dispute-spike")
        assert len(scenario.validations) >= 1
        assert any(v.check == "orders_count" for v in scenario.validations)
        assert len(scenario.findings) >= 1

    def test_payment_decline_spike_has_success_rate_param(self):
        _, scenario = load_scenario("payment-decline-spike")
        plan = compile_scenario(scenario)
        create_payments = next(s for s in plan.steps if s.action_type == "create_payments")
        assert "success_rate" in create_payments.params
        assert create_payments.params["success_rate"] == 0.7

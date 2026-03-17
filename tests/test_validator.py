"""Unit tests for validator metrics and expected evaluation (no DB)."""

from __future__ import annotations

import pytest

from carbon.validator.validator import (
    _compute_metrics,
    _evaluate_condition,
    _evaluate_expected,
)


class TestComputeMetrics:
    """Test _compute_metrics from entity_map."""

    def test_empty_entity_map(self):
        metrics = _compute_metrics({})
        assert metrics["orders_count"] == 0
        assert metrics["payments_captured_count"] == 0
        assert metrics["payments_attempted_count"] == 0
        assert metrics["payment_success_rate"] == 0.0
        assert metrics["disputes_count"] == 0
        assert metrics["refunds_count"] == 0

    def test_orders_and_captured_payments(self):
        entity_map = {
            "order_0": {"entity_type": "order", "state": "created"},
            "order_1": {"entity_type": "order", "state": "created"},
            "payment_0": {"entity_type": "payment", "state": "captured"},
            "payment_1": {"entity_type": "payment", "state": "authorized"},
        }
        metrics = _compute_metrics(entity_map)
        assert metrics["orders_count"] == 2
        assert metrics["payments_captured_count"] == 1
        assert metrics["payments_attempted_count"] == 2
        assert metrics["payment_success_rate"] == 0.5
        assert metrics["disputes_count"] == 0
        assert metrics["refunds_count"] == 0

    def test_disputes_and_refunds(self):
        entity_map = {
            "order_0": {"entity_type": "order"},
            "payment_0": {"entity_type": "payment", "state": "captured"},
            "dispute_0": {"entity_type": "dispute"},
            "refund_0": {"entity_type": "refund"},
            "refund_1": {"entity_type": "refund"},
        }
        metrics = _compute_metrics(entity_map)
        assert metrics["orders_count"] == 1
        assert metrics["payments_captured_count"] == 1
        assert metrics["disputes_count"] == 1
        assert metrics["refunds_count"] == 2


class TestEvaluateExpected:
    """Test _evaluate_expected(actual, expected)."""

    def test_numeric_equal(self):
        passed, msg = _evaluate_expected(1000, 1000)
        assert passed is True
        assert "1000" in msg

    def test_string_predicate_gte(self):
        passed, msg = _evaluate_expected(1000, ">= 1000")
        assert passed is True
        passed, _ = _evaluate_expected(999, ">= 1000")
        assert passed is False

    def test_string_predicate_gt_rate(self):
        passed, _ = _evaluate_expected(0.7, "> 0.65")
        assert passed is True
        passed, _ = _evaluate_expected(0.6, "> 0.65")
        assert passed is False

    def test_boolean_expected(self):
        passed, _ = _evaluate_expected(1, True)
        assert passed is True
        passed, _ = _evaluate_expected(0, False)
        assert passed is True


class TestEvaluateCondition:
    """Test _evaluate_condition(condition, metrics)."""

    def test_orders_count_gte(self):
        metrics = {"orders_count": 1000, "payments_captured_count": 500}
        assert _evaluate_condition("orders_count >= 1000", metrics) is True
        assert _evaluate_condition("orders_count >= 1001", metrics) is False

    def test_payment_success_rate_lt(self):
        metrics = {"payment_success_rate": 0.4, "orders_count": 100}
        assert _evaluate_condition("payment_success_rate < 0.5", metrics) is True
        assert _evaluate_condition("payment_success_rate < 0.3", metrics) is False

    def test_unknown_metric_returns_false(self):
        metrics = {"orders_count": 10}
        assert _evaluate_condition("unknown_metric >= 1", metrics) is False

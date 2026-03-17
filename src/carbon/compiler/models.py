"""Execution plan model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Step:
    """Single executable step (one adapter call or N calls when expanded)."""

    phase: str
    action_type: str
    params: dict[str, Any]
    # For for_each expansion: list of entity refs this step applies to (e.g. order_0, order_1)
    refs: list[str] | None = None  # if set, step is expanded per ref


@dataclass
class ExecutionPlan:
    steps: list[Step]
    scenario_name: str
    parameters: dict[str, Any]

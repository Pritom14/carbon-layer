"""Compile scenario into linear execution plan (MVP: no full DAG)."""

from __future__ import annotations

from typing import Any

from carbon.compiler.models import ExecutionPlan, Step
from carbon.scenarios.models import Scenario


def compile_scenario(scenario: Scenario, overrides: dict[str, Any] | None = None) -> ExecutionPlan:
    """Produce an ordered list of steps. For_each is resolved by executor using entity_map.

    overrides: optional dict of parameter key/value pairs that override scenario defaults.
    Unknown keys produce a warning but do not fail.
    """
    parameters = dict(scenario.parameters)
    if overrides:
        for key, value in overrides.items():
            if key not in parameters:
                print(f"Warning: unknown parameter override '{key}' -- ignoring")
            else:
                parameters[key] = value

    steps: list[Step] = []
    for phase in scenario.phases:
        for action in phase.actions:
            step = Step(
                phase=phase.name,
                action_type=action.action_type,
                params=dict(action.params),
                refs=None,
            )
            steps.append(step)
    return ExecutionPlan(
        steps=steps,
        scenario_name=scenario.name,
        parameters=parameters,
    )

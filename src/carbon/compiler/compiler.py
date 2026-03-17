"""Compile scenario into linear execution plan (MVP: no full DAG)."""

from carbon.compiler.models import ExecutionPlan, Step
from carbon.scenarios.models import Scenario


def compile_scenario(scenario: Scenario) -> ExecutionPlan:
    """Produce an ordered list of steps. For_each is resolved by executor using entity_map."""
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
        parameters=scenario.parameters,
    )

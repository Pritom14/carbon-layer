"""Compile scenario into linear execution plan (MVP: no full DAG)."""

from __future__ import annotations

import ast
import operator
import re
from typing import Any

from carbon.compiler.models import ExecutionPlan, Step
from carbon.scenarios.models import Scenario


_REF = re.compile(r"^\$\{(.+)\}$")
_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


def _eval_expr(expr: str, parameters: dict[str, Any]) -> Any:
    """Evaluate `${...}` arithmetic over scenario parameters (names, numbers, + - * /)."""

    def ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in parameters:
                raise ValueError(f"unknown parameter '{node.id}' in ${{{expr}}}")
            return parameters[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        raise ValueError(f"unsupported expression: ${{{expr}}}")

    return ev(ast.parse(expr.strip(), mode="eval"))


def _resolve(value: Any, parameters: dict[str, Any], key: str = "") -> Any:
    if isinstance(value, str):
        m = _REF.match(value.strip())
        if m:
            result = _eval_expr(m.group(1), parameters)
            # Counts are entity counts; round derived values like rate * orders
            if key == "count":
                return max(0, int(round(result)))
            return result
    return value


_INLINE_REF = re.compile(r"\$\{([^}]+)\}")


def render_expected(expected: Any, parameters: dict[str, Any]) -> Any:
    """Substitute `${...}` inside validation thresholds, e.g. ">= ${baseline_orders}"."""
    if not isinstance(expected, str) or "${" not in expected:
        return expected

    def sub(m: re.Match) -> str:
        val = _eval_expr(m.group(1), parameters)
        return str(int(round(val))) if isinstance(val, float) and val.is_integer() else str(val)

    return _INLINE_REF.sub(sub, expected)


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
                params={k: _resolve(v, parameters, k) for k, v in action.params.items()},
                refs=None,
            )
            steps.append(step)
    return ExecutionPlan(
        steps=steps,
        scenario_name=scenario.name,
        parameters=parameters,
    )

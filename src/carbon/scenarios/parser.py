"""Parse scenario YAML into Scenario model."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from carbon.scenarios.models import ActionDef, Phase, Scenario, Validation


def _parse_actions(actions: list[Any]) -> list[ActionDef]:
    result = []
    for item in actions:
        if isinstance(item, dict):
            for action_type, params in item.items():
                result.append(ActionDef(action_type=action_type, params=params or {}))
        else:
            raise ValueError(f"Invalid action item: {item}")
    return result


def parse_scenario(content: str | dict) -> Scenario:
    if isinstance(content, str):
        data = yaml.safe_load(content) or {}
    else:
        data = content
    name = data.get("name", "unnamed")
    description = data.get("description", "")
    category = data.get("category", "")
    parameters = data.get("parameters", {})
    # Resolve parameter defaults for MVP
    params_resolved = {}
    for k, v in parameters.items():
        if isinstance(v, dict) and "default" in v:
            params_resolved[k] = v["default"]
        else:
            params_resolved[k] = v
    phases = []
    for p in data.get("phases", []):
        if isinstance(p, dict):
            phases.append(
                Phase(
                    name=p.get("name", ""),
                    description=p.get("description", ""),
                    depends_on=p.get("depends_on"),
                    actions=_parse_actions(p.get("actions", [])),
                    wait=p.get("wait"),
                )
            )
    validations = []
    for v in data.get("validations", []):
        if isinstance(v, dict) and "check" in v:
            validations.append(
                Validation(
                    check=v["check"],
                    description=v.get("description", ""),
                    expected=v.get("expected"),
                    severity=v.get("severity", "high"),
                )
            )
    findings = data.get("findings", [])
    if not isinstance(findings, list):
        findings = []
    return Scenario(
        name=name,
        description=description,
        category=category,
        parameters=params_resolved,
        phases=phases,
        validations=validations,
        findings=findings,
    )

"""Scenario model (YAML → in-memory)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ActionDef:
    """Single action in a phase, e.g. create_orders: { count: 10 }."""

    action_type: str  # e.g. create_orders, create_payments
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Phase:
    name: str
    description: str = ""
    depends_on: str | None = None
    actions: list[ActionDef] = field(default_factory=list)
    wait: str | None = None  # e.g. "10s"


@dataclass
class Validation:
    check: str
    description: str = ""
    expected: Any = None
    severity: str = "high"


@dataclass
class Scenario:
    name: str
    description: str = ""
    category: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    phases: list[Phase] = field(default_factory=list)
    validations: list[Validation] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)

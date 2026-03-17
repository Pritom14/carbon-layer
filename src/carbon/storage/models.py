"""Storage models (dataclasses for DB rows)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class Run:
    id: str
    scenario_name: str
    provider: str
    parameters: dict[str, Any]
    status: str  # pending | running | completed | failed
    started_at: datetime | None
    completed_at: datetime | None
    summary: dict[str, Any] | None


@dataclass
class Action:
    id: str
    run_id: str
    phase: str
    action_type: str
    parameters: dict[str, Any]
    dependencies: list[str] | None
    status: str
    result: dict[str, Any] | None
    error: str | None
    executed_at: datetime | None
    retry_count: int


@dataclass
class EntityMapping:
    run_id: str
    local_id: str
    entity_type: str
    remote_id: str | None
    provider: str
    state: str | None
    metadata: dict[str, Any] | None
    created_at: datetime | None


@dataclass
class Finding:
    id: str
    run_id: str
    check_name: str
    severity: str  # critical | high | medium | low
    passed: bool
    message: str | None
    details: dict[str, Any] | None
    created_at: datetime | None

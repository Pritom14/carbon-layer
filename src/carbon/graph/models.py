"""Domain graph models (simplified for MVP)."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StateNode:
    entity: str
    state: str

    @property
    def key(self) -> str:
        return f"{self.entity}.{self.state}"


@dataclass
class Transition:
    from_state: str  # e.g. "order.created"
    to_state: str
    on: str


@dataclass
class EntityGraph:
    """In-memory graph of entities and transitions. MVP: no NetworkX."""

    entities: dict[str, list[str]] = field(default_factory=dict)  # entity_id -> [states]
    transitions: list[Transition] = field(default_factory=list)

    def get_states(self, entity: str) -> list[str]:
        return self.entities.get(entity, [])

    def can_transition(self, from_state: str, to_state: str, on: str) -> bool:
        for t in self.transitions:
            if t.from_state == from_state and t.to_state == to_state and t.on == on:
                return True
        return False

"""Load domain graph from YAML."""

from __future__ import annotations

from pathlib import Path

import yaml

from carbon.graph.models import EntityGraph, Transition


def load_domain_graph(path: Path | None = None) -> EntityGraph:
    if path is None:
        # Default: domains/payment.yaml relative to package or cwd
        base = Path(__file__).resolve().parent.parent.parent.parent
        path = base / "domains" / "payment.yaml"
    if not path.exists():
        return EntityGraph()
    data = yaml.safe_load(path.read_text()) or {}
    entities = {e["id"]: e.get("states", []) for e in data.get("entities", []) if isinstance(e, dict) and "id" in e}
    transitions = []
    for t in data.get("transitions", []):
        if isinstance(t, dict) and "from" in t and "to" in t and "on" in t:
            transitions.append(
                Transition(
                    from_state=t["from"],
                    to_state=t["to"],
                    on=t["on"],
                )
            )
    return EntityGraph(entities=entities, transitions=transitions)

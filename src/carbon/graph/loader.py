"""Load domain graph from YAML."""

from __future__ import annotations

from pathlib import Path

import yaml

from carbon.graph.models import EntityGraph, Transition


def load_domain_graph(path: Path | None = None) -> EntityGraph:
    if path is None:
        # Default: payment domain graph shipped inside the package
        path = Path(__file__).resolve().parent.parent / "data" / "domains" / "payment.yaml"
    if not path.exists():
        return EntityGraph()
    data = yaml.safe_load(path.read_text()) or {}
    entities = {e["id"]: e.get("states", []) for e in data.get("entities", []) if isinstance(e, dict) and "id" in e}
    transitions = []
    for t in data.get("transitions", []):
        if not isinstance(t, dict):
            continue
        # YAML 1.1 parses a bare `on:` key as boolean True
        on = t.get("on", t.get(True))
        if "from" in t and "to" in t and on is not None:
            transitions.append(
                Transition(
                    from_state=t["from"],
                    to_state=t["to"],
                    on=on,
                )
            )
    return EntityGraph(entities=entities, transitions=transitions)

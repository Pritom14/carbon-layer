"""Discover and load scenarios from disk."""

from __future__ import annotations

from pathlib import Path

from carbon.config import get_settings
from carbon.scenarios.models import Scenario
from carbon.scenarios.parser import parse_scenario


def _scenarios_dir() -> Path:
    return get_settings().carbon_scenarios_dir


def _find_yaml_files(dir_path: Path) -> list[Path]:
    if not dir_path.exists():
        return []
    return list(dir_path.rglob("*.yaml")) + list(dir_path.rglob("*.yml"))


def list_scenarios() -> list[str]:
    """Return scenario names (from 'name' in each YAML)."""
    names = []
    seen = set()
    for path in _find_yaml_files(_scenarios_dir()):
        try:
            content = path.read_text()
            scenario = parse_scenario(content)
            if scenario.name and scenario.name not in seen:
                names.append(scenario.name)
                seen.add(scenario.name)
        except Exception:
            continue
    # If no dir or empty, fallback to built-in path relative to repo
    if not names:
        base = Path(__file__).resolve().parent.parent.parent
        builtin = base / "scenarios"
        for path in _find_yaml_files(builtin):
            try:
                content = path.read_text()
                scenario = parse_scenario(content)
                if scenario.name and scenario.name not in seen:
                    names.append(scenario.name)
                    seen.add(scenario.name)
            except Exception:
                continue
    return sorted(names)


def load_scenario(name: str) -> tuple[Path | None, Scenario]:
    """Load scenario by name. Returns (path, Scenario) or raises LookupError."""

    for dir_path in [_scenarios_dir(), Path(__file__).resolve().parent.parent.parent / "scenarios"]:
        for path in _find_yaml_files(dir_path):
            try:
                content = path.read_text()
                scenario = parse_scenario(content)
                if scenario.name == name:
                    return path, scenario
            except Exception:
                continue
    raise LookupError(f"Scenario not found: {name}")

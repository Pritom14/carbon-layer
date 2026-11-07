"""Test AI agents that order and pay through MCP tools (carbon agent)."""

from carbon.agent.runner import evaluate, run_scenario
from carbon.agent.shop import SCENARIOS, AgentScenario, Shop

__all__ = ["SCENARIOS", "AgentScenario", "Shop", "evaluate", "run_scenario"]

"""Built-in data must resolve from any working directory (i.e. after pip install)."""

from carbon.graph.loader import load_domain_graph
from carbon.scenarios.registry import list_scenarios, load_scenario


def test_builtin_scenarios_found_outside_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    names = list_scenarios()
    assert "dispute-spike" in names
    assert len(names) >= 11
    path, scenario = load_scenario("dispute-spike")
    assert scenario.name == "dispute-spike"


def test_domain_graph_found_outside_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    graph = load_domain_graph()
    assert graph.entities
    assert graph.transitions

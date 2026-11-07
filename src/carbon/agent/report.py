"""Terminal output for carbon agent."""

from __future__ import annotations

from typing import Any, Dict, List

from rich.console import Console
from rich.table import Table

_STYLE = {"pass": "green", "fail": "red", "inconclusive": "yellow", "warn": "yellow"}


def _grid() -> Table:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", no_wrap=True)
    grid.add_column(overflow="fold")
    return grid


def print_result(console: Console, r: Dict[str, Any]) -> None:
    console.print(f"[bold]carbon agent[/bold]  [cyan]{r['scenario']}[/cyan]")
    console.print(f"[dim]{r['summary']}[/dim]")
    grid = _grid()
    agent = r["agent"]
    if agent.get("command"):
        if agent["timed_out"]:
            ran = f"timed out after {agent['seconds']} s"
        else:
            ran = f"exit {agent['exit_code']} in {agent['seconds']} s"
        grid.add_row("agent", f"{agent['command']}\n[dim]{ran}[/dim]")
    calls = ", ".join(f"{k} {v}" for k, v in r["tool_calls"].items()) or "none"
    grid.add_row("calls", calls)
    orders = r["orders"]
    grid.add_row("orders", str(len(orders)) + (
        "  (" + ", ".join(o["status"] for o in orders) + ")" if orders else ""))
    console.print(grid)
    findings: List[Dict[str, Any]] = r["findings"]
    out = _grid()
    if r["status"] == "pass":
        out.add_row("[green]PASS[/green]", r["outcome"])
    for f in findings:
        style = _STYLE[f["severity"]]
        label = "WARN" if f["severity"] == "warn" else f["severity"].upper()
        out.add_row(f"[{style}]{label}[/{style}]", f["message"])
        if f.get("fix") and f["severity"] == "fail":
            out.add_row("Fix", f["fix"])
    console.print()
    console.print(out)
    if agent.get("command") and (agent["timed_out"] or agent["exit_code"]) and agent["output_tail"]:
        console.print("[dim]agent output (last lines):[/dim]")
        for line in agent["output_tail"][-5:]:
            console.print(f"[dim]  {line}[/dim]", markup=False, highlight=False)


def print_summary(console: Console, results: List[Dict[str, Any]]) -> None:
    failed = sum(r["status"] != "pass" for r in results)
    head = f"{len(results)} scenarios: " + (f"{failed} failed" if failed else "all passed")
    console.print(f"[bold]{head}[/bold]")
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True)
    grid.add_column(no_wrap=True)
    grid.add_column(overflow="fold")
    for r in results:
        style = _STYLE[r["status"]]
        grid.add_row(f"[{style}]{r['status'].upper()}[/{style}]", r["scenario"], f"[dim]{r['outcome']}[/dim]")
    console.print(grid)

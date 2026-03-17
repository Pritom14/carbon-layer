"""Carbon CLI — chaos engineering for payment flows."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from carbon.adapters import get_adapter
from carbon.compiler import compile_scenario
from carbon.config import get_settings
from carbon.engine import run_plan
from carbon.reporter import print_report
from carbon.scenarios import list_scenarios, load_scenario
from carbon.storage.repo import create_run, ensure_db, update_run_status
from carbon.validator import run_validations
from carbon.webhook import send_webhooks

app = typer.Typer(name="carbon", help="Chaos engineering for payment flows.")
console = Console()


@app.command()
def scenarios_list() -> None:
    """List available scenarios."""
    names = list_scenarios()
    if not names:
        console.print("[yellow]No scenarios found. Add YAML files under scenarios/ or set CARBON_SCENARIOS_DIR.[/yellow]")
        return
    for n in names:
        console.print(f"  • {n}")


@app.command()
def run(
    scenario_name: str = typer.Argument(..., help="Scenario name (e.g. dispute-spike)"),
    provider: str = typer.Option("mock", "--provider", "-p", help="Provider: mock or razorpay"),
    api_key: Optional[str] = typer.Option(None, "--api-key", envvar="RAZORPAY_API_KEY"),
    api_secret: Optional[str] = typer.Option(None, "--api-secret", envvar="RAZORPAY_API_SECRET"),
    webhook_url: Optional[str] = typer.Option(
        None, "--webhook-url", help="POST Razorpay-format webhook events to this URL after the run"
    ),
    webhook_secret: Optional[str] = typer.Option(
        None,
        "--webhook-secret",
        help="Secret used to compute X-Razorpay-Signature for webhook simulation",
    ),
) -> None:
    """Run a scenario and print report."""
    settings = get_settings()
    if provider == "razorpay" and not (api_key and api_secret):
        api_key = settings.razorpay_api_key
        api_secret = settings.razorpay_api_secret
    if provider == "razorpay" and not (api_key and api_secret):
        console.print("[yellow]Razorpay keys not set. Using mock adapter.[/yellow]")
        provider = "mock"
    try:
        _, scenario = load_scenario(scenario_name)
    except LookupError:
        console.print(f"[red]Scenario not found: {scenario_name}[/red]")
        raise typer.Exit(1)
    plan = compile_scenario(scenario)
    adapter = get_adapter(provider, api_key=api_key, api_secret=api_secret)
    run_id = asyncio.run(_do_run(plan, scenario_name, provider, adapter, webhook_url, webhook_secret))
    console.print(f"\n[green]Run completed: {run_id}[/green]")
    asyncio.run(print_report(run_id))


async def _do_run(
    plan,
    scenario_name: str,
    provider: str,
    adapter,
    webhook_url: Optional[str],
    webhook_secret: Optional[str],
) -> str:
    await ensure_db()
    run_id = await create_run(scenario_name, provider, plan.parameters)
    try:
        await run_plan(plan, run_id, provider, adapter)
        if webhook_url:
            await send_webhooks(run_id, target_url=webhook_url, secret=webhook_secret or "carbon")
        await run_validations(run_id)
    except Exception as e:
        await update_run_status(run_id, "failed")
        raise e
    return run_id


@app.command()
def report(
    run_id: str = typer.Argument(..., help="Run ID from a previous run"),
) -> None:
    """Show report for a run."""
    asyncio.run(print_report(run_id))


if __name__ == "__main__":
    app()

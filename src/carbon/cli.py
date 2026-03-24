"""Carbon CLI — chaos engineering for payment flows."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from carbon.adapters import get_adapter
from carbon.callback import post_run_callback
from carbon.compiler import compile_scenario
from carbon.config import get_settings
from carbon.engine import run_plan
from carbon.reporter import print_report
from carbon.reporter.html import generate_html_report
from carbon.scenarios import list_scenarios, load_scenario
from carbon.storage.repo import create_run, ensure_db, update_run_status
from carbon.validator import run_validations
from carbon.webhook import replay_webhooks, send_webhooks

app = typer.Typer(name="carbon", help="Chaos engineering for payment flows.")
console = Console()


PRO_SCENARIOS = [
    ("rbi-compliance-check", "RBI payment regulation compliance validation"),
    ("pci-readiness-suite", "PCI DSS readiness checks for payment handlers"),
    ("load-test-10k-tps", "10,000 TPS sustained load with webhook delivery"),
    ("multi-gateway-failover", "Cross-provider failover under gateway outage"),
    ("settlement-reconciliation", "End-to-end settlement and reconciliation stress test"),
]


@app.command()
def scenarios_list() -> None:
    """List available scenarios."""
    names = list_scenarios()
    if not names:
        console.print("[yellow]No scenarios found. Add YAML files under scenarios/ or set CARBON_SCENARIOS_DIR.[/yellow]")
        return
    for n in names:
        console.print(f"  • {n}")
    console.print("")
    console.print("[dim]Pro scenarios (coming soon):[/dim]")
    for name, desc in PRO_SCENARIOS:
        console.print(f"  [dim](locked) {name} — {desc}[/dim]")
    console.print(f"\n[dim]Join the waitlist → pritom14.github.io/carbon-layer/waitlist[/dim]")


@app.command()
def run(
    scenario_name: str = typer.Argument(..., help="Scenario name (e.g. dispute-spike)"),
    provider: str = typer.Option("mock", "--provider", "-p", help="Provider: mock, razorpay, stripe, cashfree, or juspay"),
    api_key: Optional[str] = typer.Option(None, "--api-key", envvar="RAZORPAY_API_KEY"),
    api_secret: Optional[str] = typer.Option(None, "--api-secret", envvar="RAZORPAY_API_SECRET"),
    stripe_key: Optional[str] = typer.Option(None, "--stripe-key", envvar="STRIPE_API_KEY"),
    cashfree_id: Optional[str] = typer.Option(None, "--cashfree-id", envvar="CASHFREE_CLIENT_ID"),
    cashfree_secret: Optional[str] = typer.Option(None, "--cashfree-secret", envvar="CASHFREE_CLIENT_SECRET"),
    juspay_key: Optional[str] = typer.Option(None, "--juspay-key", envvar="JUSPAY_API_KEY"),
    juspay_merchant_id: Optional[str] = typer.Option(None, "--juspay-merchant-id", envvar="JUSPAY_MERCHANT_ID"),
    webhook_url: Optional[str] = typer.Option(
        None, "--webhook-url", help="POST webhook events to this URL after the run"
    ),
    webhook_secret: Optional[str] = typer.Option(
        None,
        "--webhook-secret",
        help="Secret used to sign webhook payloads (HMAC-SHA256)",
    ),
    set_params: Optional[list[str]] = typer.Option(
        None, "--set", help="Override scenario parameters. Format: key=value. Can be used multiple times."
    ),
    callback_url: Optional[str] = typer.Option(
        None, "--callback-url", help="POST run summary JSON to this URL after completion (for CI/CD integration)"
    ),
    ci: bool = typer.Option(False, "--ci", help="Exit with code 1 if any webhook returned 5xx or timed out"),
    webhook_repeat: int = typer.Option(1, "--webhook-repeat", help="Fire each webhook N times to test idempotency"),
    webhook_order: str = typer.Option("sequence", "--webhook-order", help="Webhook delivery order: sequence, reverse, or random"),
    webhook_signature: str = typer.Option("valid", "--webhook-signature", help="Signature mode: valid, missing, corrupted, or wrong_secret"),
) -> None:
    """Run a scenario and print report."""
    settings = get_settings()

    # Resolve credentials per provider
    if provider == "razorpay" and not (api_key and api_secret):
        api_key = settings.razorpay_api_key
        api_secret = settings.razorpay_api_secret
    if provider == "razorpay" and not (api_key and api_secret):
        console.print("[yellow]Razorpay keys not set. Using mock adapter.[/yellow]")
        provider = "mock"

    if provider == "stripe" and not stripe_key:
        stripe_key = settings.stripe_api_key
    if provider == "stripe" and not stripe_key:
        console.print("[yellow]Stripe key not set. Using mock adapter.[/yellow]")
        provider = "mock"

    if provider == "cashfree" and not (cashfree_id and cashfree_secret):
        cashfree_id = settings.cashfree_client_id
        cashfree_secret = settings.cashfree_client_secret
    if provider == "cashfree" and not (cashfree_id and cashfree_secret):
        console.print("[yellow]Cashfree credentials not set. Using mock adapter.[/yellow]")
        provider = "mock"

    if provider == "juspay" and not (juspay_key and juspay_merchant_id):
        juspay_key = settings.juspay_api_key
        juspay_merchant_id = settings.juspay_merchant_id
    if provider == "juspay" and not (juspay_key and juspay_merchant_id):
        console.print("[yellow]Juspay credentials not set. Using mock adapter.[/yellow]")
        provider = "mock"

    # Map provider-specific keys to the adapter registry's api_key/api_secret params
    if provider == "stripe":
        effective_key = stripe_key
    elif provider == "cashfree":
        effective_key = cashfree_id
        api_secret = cashfree_secret
    elif provider == "juspay":
        effective_key = juspay_key
        api_secret = juspay_merchant_id
    else:
        effective_key = api_key

    try:
        _, scenario = load_scenario(scenario_name)
    except LookupError:
        console.print(f"[red]Scenario not found: {scenario_name}[/red]")
        raise typer.Exit(1)
    overrides: dict = {}
    if set_params:
        for param in set_params:
            if "=" not in param:
                console.print(f"[yellow]Ignoring invalid --set value (expected key=value): {param}[/yellow]")
                continue
            key, _, raw_value = param.partition("=")
            # Attempt type coercion: int, float, then string
            for cast in (int, float):
                try:
                    overrides[key.strip()] = cast(raw_value)
                    break
                except ValueError:
                    continue
            else:
                overrides[key.strip()] = raw_value
    plan = compile_scenario(scenario, overrides=overrides or None)
    adapter = get_adapter(provider, api_key=effective_key, api_secret=api_secret)
    run_id = asyncio.run(_do_run(
        plan, scenario_name, provider, adapter, webhook_url, webhook_secret, callback_url,
        webhook_repeat=webhook_repeat, webhook_order=webhook_order, webhook_signature=webhook_signature,
    ))
    console.print(f"\n[green]Run completed: {run_id}[/green]")
    asyncio.run(print_report(run_id))

    if ci and webhook_url:
        from carbon.storage.repo import get_webhook_deliveries
        deliveries = asyncio.run(get_webhook_deliveries(run_id))
        failures = [
            d for d in deliveries
            if d.get("status_code") is None or (isinstance(d.get("status_code"), int) and d["status_code"] >= 500)
        ]
        if failures:
            console.print(f"[red]CI check failed: {len(failures)} webhook(s) returned 5xx or timed out.[/red]")
            raise typer.Exit(1)


async def _do_run(
    plan,
    scenario_name: str,
    provider: str,
    adapter,
    webhook_url: Optional[str],
    webhook_secret: Optional[str],
    callback_url: Optional[str] = None,
    webhook_repeat: int = 1,
    webhook_order: str = "sequence",
    webhook_signature: str = "valid",
) -> str:
    await ensure_db()
    run_id = await create_run(scenario_name, provider, plan.parameters)
    try:
        await run_plan(plan, run_id, provider, adapter)
        if webhook_url:
            await send_webhooks(
                run_id,
                target_url=webhook_url,
                secret=webhook_secret or "carbon",
                provider=provider,
                repeat=webhook_repeat,
                order=webhook_order,
                signature_mode=webhook_signature,
            )
        await run_validations(run_id)
        if callback_url:
            result = await post_run_callback(run_id, callback_url)
            if result["ok"]:
                console.print(f"[dim]Callback delivered: {result['status_code']}[/dim]")
            else:
                console.print(f"[yellow]Callback failed: {result.get('error') or result.get('status_code')}[/yellow]")
    except Exception as e:
        await update_run_status(run_id, "failed")
        raise e
    return run_id


@app.command()
def replay(
    run_id: str = typer.Argument(..., help="Run ID to replay webhooks from"),
    webhook_url: str = typer.Option(..., "--webhook-url", help="Target URL to replay webhooks to"),
    provider: str = typer.Option("mock", "--provider", "-p", help="Provider for signing"),
    webhook_secret: Optional[str] = typer.Option(None, "--webhook-secret", help="Secret for signing"),
) -> None:
    """Replay webhook payloads from a previous run."""
    async def _replay() -> list:
        await ensure_db()
        return await replay_webhooks(
            run_id,
            target_url=webhook_url,
            secret=webhook_secret or "carbon",
            provider=provider,
        )
    deliveries = asyncio.run(_replay())
    if not deliveries:
        console.print(f"[yellow]No stored webhook payloads found for run {run_id}.[/yellow]")
        console.print("[dim]Payloads are stored starting from v0.6. Older runs cannot be replayed.[/dim]")
        return
    ok_count = sum(1 for d in deliveries if d.get("ok"))
    fail_count = len(deliveries) - ok_count
    console.print(f"[green]Replayed {len(deliveries)} webhooks: {ok_count} succeeded, {fail_count} failed.[/green]")


@app.command()
def report(
    run_id: str = typer.Argument(..., help="Run ID from a previous run"),
    format: str = typer.Option("terminal", "--format", help="Output format: terminal or html"),
) -> None:
    """Show report for a run."""
    if format == "pdf":
        console.print(
            "[yellow]PDF export is a Carbon Layer Pro feature.[/yellow]\n"
            "[dim]Pro includes PDF reports, scheduled runs, compliance dashboards, and more.[/dim]\n"
            "[dim]Join the waitlist → pritom14.github.io/carbon-layer/waitlist[/dim]"
        )
        raise typer.Exit(0)
    if format == "html":
        import asyncio as _asyncio
        from pathlib import Path
        html = _asyncio.run(generate_html_report(run_id))
        out_path = Path(f"carbon_report_{run_id}.html")
        out_path.write_text(html, encoding="utf-8")
        console.print(f"Report written to {out_path}")
    else:
        asyncio.run(print_report(run_id))


if __name__ == "__main__":
    app()

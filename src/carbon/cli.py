"""Carbon CLI — chaos engineering for payment flows."""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Optional

import typer
from rich.console import Console

from carbon import __version__
from carbon.adapters import get_adapter
from carbon.callback import post_run_callback
from carbon.compiler import compile_scenario
from carbon.config import get_settings
from carbon.engine import run_plan
from carbon.reporter import print_report
from carbon.reporter.html import generate_html_report
from carbon.scenarios import list_scenarios, load_scenario
from carbon.storage.repo import create_run, ensure_db, insert_finding, update_run_status
from carbon.validator import run_validations
from carbon.webhook import replay_webhooks, send_webhooks
from carbon.webhook.checks import delivery_hints, evaluate_deliveries, nothing_listening, overall_passed
from carbon.webhook.verify import VerifyError, evaluate, read_counter

app = typer.Typer(
    name="carbon",
    help="Chaos engineering for payment flows.",
    epilog="Carbon Layer Pro waitlist: pritom14.github.io/carbon-layer/waitlist",
)
# Human-readable output. Switched to stderr for --output json so stdout stays one JSON document.
console = Console()
err_console = Console(stderr=True)

# Exit code when every webhook failed to connect (nothing listening on --webhook-url)
EXIT_NOTHING_LISTENING = 3


class Provider(str, Enum):
    mock = "mock"
    razorpay = "razorpay"
    stripe = "stripe"
    cashfree = "cashfree"
    juspay = "juspay"
    hyperswitch = "hyperswitch"


class WebhookOrder(str, Enum):
    sequence = "sequence"
    reverse = "reverse"
    random = "random"


class SignatureMode(str, Enum):
    valid = "valid"
    missing = "missing"
    corrupted = "corrupted"
    wrong_secret = "wrong_secret"


class OutputFormat(str, Enum):
    terminal = "terminal"
    json = "json"


class ReportFormat(str, Enum):
    terminal = "terminal"
    html = "html"


_DISPLAY_NAME = {
    "razorpay": "Razorpay",
    "stripe": "Stripe",
    "cashfree": "Cashfree",
    "juspay": "Juspay",
    "hyperswitch": "Hyperswitch",
}


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"carbon-layer {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show the version and exit."
    ),
) -> None:
    """Chaos engineering for payment flows."""
    console.stderr = False


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
    provider: Provider = typer.Option(
        Provider.mock, "--provider", "-p", help="Webhook format to simulate (mock sends Razorpay format)"
    ),
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
    ci: bool = typer.Option(
        False,
        "--ci",
        help="Exit with code 1 if any graded check fails: a 5xx/timeout, a bad signature accepted "
        "with 2xx, or a failed --verify-cmd",
    ),
    webhook_repeat: int = typer.Option(1, "--webhook-repeat", help="Fire each webhook N times to test idempotency"),
    webhook_order: WebhookOrder = typer.Option(
        WebhookOrder.sequence, "--webhook-order", help="Webhook delivery order"
    ),
    webhook_signature: SignatureMode = typer.Option(
        SignatureMode.valid,
        "--webhook-signature",
        help="Signature mode. With missing/corrupted/wrong_secret your handler must reject every "
        "webhook (400/401/403); any 2xx is a failure.",
    ),
    output: OutputFormat = typer.Option(OutputFormat.terminal, "--output", help="Output format"),
    verify_cmd: Optional[str] = typer.Option(
        None,
        "--verify-cmd",
        help="Shell command printing a number that counts your handler's side effects "
        "(e.g. a SQL count of wallet credits). Run before and after delivery to detect "
        "duplicate or lost processing.",
    ),
    verify_event: str = typer.Option(
        "payment.captured", "--verify-event", help="Event type whose side effect --verify-cmd counts"
    ),
    client_verify_url: Optional[str] = typer.Option(
        None,
        "--client-verify-url",
        help="Razorpay only: also POST a Checkout-style verify request (razorpay_order_id, "
        "razorpay_payment_id, razorpay_signature) here for every captured payment, before the "
        "webhooks. With --verify-cmd, checks one side effect per payment across both paths.",
    ),
    client_verify_secret: Optional[str] = typer.Option(
        None,
        "--client-verify-secret",
        envvar="RAZORPAY_KEY_SECRET",
        help="API key secret used to sign client verify requests (defaults to --webhook-secret)",
    ),
    client_verify_format: str = typer.Option(
        "json", "--client-verify-format", help="Body format for client verify requests: json or form"
    ),
) -> None:
    """Run a scenario and print report."""
    provider = provider.value
    webhook_order = webhook_order.value
    webhook_signature = webhook_signature.value
    output = output.value
    # With --output json, stdout carries only the JSON document; everything else goes to stderr.
    console.stderr = output == "json"
    settings = get_settings()
    requested_provider = provider
    # Webhook format/signing follows the requested provider even when payments are simulated.
    webhook_provider = "razorpay" if provider == "mock" else provider

    # Resolve credentials per provider
    if provider == "razorpay" and not (api_key and api_secret):
        api_key = settings.razorpay_api_key
        api_secret = settings.razorpay_api_secret
    if provider == "stripe" and not stripe_key:
        stripe_key = settings.stripe_api_key
    if provider == "cashfree" and not (cashfree_id and cashfree_secret):
        cashfree_id = settings.cashfree_client_id
        cashfree_secret = settings.cashfree_client_secret
    if provider == "juspay" and not (juspay_key and juspay_merchant_id):
        juspay_key = settings.juspay_api_key
        juspay_merchant_id = settings.juspay_merchant_id

    has_keys = {
        "razorpay": bool(api_key and api_secret),
        "stripe": bool(stripe_key),
        "cashfree": bool(cashfree_id and cashfree_secret),
        "juspay": bool(juspay_key and juspay_merchant_id),
    }.get(provider, False)
    # Razorpay and Juspay cannot create payments or disputes server-side, and Hyperswitch
    # only sends merchants webhooks, so those always simulate locally. Webhook testing never
    # needs gateway keys.
    if provider in ("mock", "razorpay", "juspay", "hyperswitch") or not has_keys:
        note = ""
        if has_keys:
            note = f" ({_DISPLAY_NAME[provider]} keys ignored: its API can't create payments or disputes)"
        err_console.print(
            f"[dim]Simulating payments locally; sending {_DISPLAY_NAME[webhook_provider]}-format "
            f"signed webhooks{note}[/dim]"
        )
        provider = "mock"

    # Map provider-specific keys to the adapter registry's api_key/api_secret params
    if provider == "stripe":
        effective_key = stripe_key
    elif provider == "cashfree":
        effective_key = cashfree_id
        api_secret = cashfree_secret
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
    if verify_cmd and not webhook_url:
        console.print("[red]--verify-cmd requires --webhook-url[/red]")
        raise typer.Exit(2)
    if client_verify_url and (webhook_provider != "razorpay" or not webhook_url):
        console.print("[red]--client-verify-url needs --provider razorpay and --webhook-url[/red]")
        raise typer.Exit(2)
    if client_verify_format not in ("json", "form"):
        console.print("[red]--client-verify-format must be json or form[/red]")
        raise typer.Exit(2)
    try:
        run_id, verification, deliveries = asyncio.run(_do_run(
            plan, scenario_name, provider, adapter, webhook_url, webhook_secret, callback_url,
            webhook_repeat=webhook_repeat, webhook_order=webhook_order, webhook_signature=webhook_signature,
            webhook_provider=webhook_provider, verify_cmd=verify_cmd, verify_event=verify_event,
            client_verify_url=client_verify_url,
            client_verify_secret=client_verify_secret or webhook_secret or "carbon",
            client_verify_format=client_verify_format,
        ))
    except VerifyError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(2)

    from carbon.storage.repo import get_findings, get_run

    findings = asyncio.run(get_findings(run_id))
    passed = overall_passed(findings, verification)
    hints = delivery_hints(deliveries, webhook_signature, webhook_url or "")

    if output == "json":
        import json as _json
        run_data = asyncio.run(get_run(run_id))

        def _count(pred) -> int:
            return sum(1 for d in deliveries if pred(d.get("status_code")))

        result = {
            "carbon_version": __version__,
            "run_id": run_id,
            "scenario": scenario_name,
            "provider": requested_provider,
            "webhook_format": webhook_provider,
            "adapter": "simulated" if provider == "mock" else "live",
            "signature_mode": webhook_signature,
            "tested": bool(deliveries),
            "status": run_data["status"] if run_data else "unknown",
            "findings": [
                {
                    "check": f["check_name"],
                    "severity": f["severity"],
                    "informational": f["severity"] == "info",
                    "passed": f["passed"],
                    "message": f.get("message"),
                }
                for f in findings
            ],
            "webhooks": {
                "total": len(deliveries),
                "ok": sum(1 for d in deliveries if d.get("ok")),
                "status_2xx": _count(lambda sc: isinstance(sc, int) and 200 <= sc < 300),
                "status_4xx": _count(lambda sc: isinstance(sc, int) and 400 <= sc < 500),
                "failed_5xx": _count(lambda sc: isinstance(sc, int) and sc >= 500),
                "timeout": _count(lambda sc: sc is None),
            },
            "verification": verification,
            "hints": hints,
            "passed": passed,
        }
        print(_json.dumps(result, indent=2, default=str))
    else:
        console.print(f"\n[green]Run completed: {run_id}[/green]")
        asyncio.run(print_report(run_id))
        if verification:
            _print_verification(verification)
    for hint in hints:
        console.print(f"[yellow]Hint: {hint}[/yellow]")

    if nothing_listening(deliveries):
        raise typer.Exit(EXIT_NOTHING_LISTENING)

    if ci and not passed:
        reasons = [f["message"] for f in findings if f["severity"] != "info" and not f["passed"]]
        if verification and not verification["passed"]:
            reasons.append("side-effect verification did not pass")
        for reason in reasons:
            console.print(f"[red]CI check failed: {reason}[/red]")
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
    webhook_provider: str = "razorpay",
    verify_cmd: Optional[str] = None,
    verify_event: str = "payment.captured",
    client_verify_url: Optional[str] = None,
    client_verify_secret: str = "carbon",
    client_verify_format: str = "json",
) -> tuple[str, Optional[dict], list[dict]]:
    await ensure_db()
    # The run records the webhook format sent, not the (usually simulated) adapter
    run_id = await create_run(scenario_name, webhook_provider, plan.parameters)
    verification = None
    deliveries: list[dict] = []
    try:
        await run_plan(plan, run_id, provider, adapter)
        if webhook_url:
            before = read_counter(verify_cmd) if verify_cmd else None
            client_calls = None
            if client_verify_url:
                from carbon.storage.repo import get_entity_map
                from carbon.webhook.client_verify import captured_payments, send_client_verifications
                from carbon.webhook.payloads import build_events_from_entity_map

                events = build_events_from_entity_map(await get_entity_map(run_id), provider="razorpay")
                client_calls = await send_client_verifications(
                    captured_payments(events),
                    url=client_verify_url,
                    key_secret=client_verify_secret,
                    fmt=client_verify_format,
                )
                ok = sum(1 for c in client_calls if c["ok"])
                console.print(f"[dim]Client verify calls: {len(client_calls)} sent, {ok} returned 2xx[/dim]")
            deliveries = await send_webhooks(
                run_id,
                target_url=webhook_url,
                secret=webhook_secret or "carbon",
                provider=webhook_provider,
                repeat=webhook_repeat,
                order=webhook_order,
                signature_mode=webhook_signature,
            )
            if verify_cmd:
                after = read_counter(verify_cmd)
                verification = evaluate(before, after, deliveries, verify_event, client_calls=client_calls)
            # Graded checks: how the handler responded
            for f in evaluate_deliveries(deliveries, webhook_signature):
                await insert_finding(run_id, f["check"], f["severity"], f["passed"], f["message"], f["details"])
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
    return run_id, verification, deliveries


def _print_verification(v: dict) -> None:
    color = "green" if v["passed"] else "red"
    label = {"pass": "PASS", "duplicates": "FAIL: duplicate processing", "lost": "FAIL: lost events"}[v["verdict"]]
    console.print(f"\n[bold]Side-effect verification[/bold] ({v['event_type']})")
    console.print(
        f"  deliveries sent: {v['deliveries']}   "
        + (f"client verify calls: {v['client_verify_calls']}   " if v.get("client_verify_calls") is not None else "")
        + f"unique {'payments' if v.get('client_verify_calls') is not None else 'events'} acked: {v['unique_events_acked']}   "
        f"counter: {v['before']:g} -> {v['after']:g} (change {v['observed_change']:g})"
    )
    console.print(f"  [{color}]{label}[/{color}]: {v['message']}")


@app.command()
def replay(
    run_id: str = typer.Argument(..., help="Run ID to replay webhooks from"),
    webhook_url: str = typer.Option(..., "--webhook-url", help="Target URL to replay webhooks to"),
    provider: Provider = typer.Option(Provider.mock, "--provider", "-p", help="Provider for signing"),
    webhook_secret: Optional[str] = typer.Option(None, "--webhook-secret", help="Secret for signing"),
) -> None:
    """Replay webhook payloads from a previous run."""
    async def _replay() -> list:
        await ensure_db()
        return await replay_webhooks(
            run_id,
            target_url=webhook_url,
            secret=webhook_secret or "carbon",
            provider="razorpay" if provider == Provider.mock else provider.value,
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
def security(
    provider: Provider = typer.Option(
        Provider.razorpay, "--provider", "-p", help="Webhook format and signing to attack with (mock = Razorpay)"
    ),
    webhook_url: str = typer.Option(..., "--webhook-url", help="Your webhook endpoint"),
    webhook_secret: str = typer.Option(
        ..., "--webhook-secret", help="The webhook secret your handler verifies with"
    ),
    client_verify_url: Optional[str] = typer.Option(
        None,
        "--client-verify-url",
        help="Razorpay only: your Checkout verify endpoint; adds forged/swapped-id checks",
    ),
    client_verify_secret: Optional[str] = typer.Option(
        None,
        "--client-verify-secret",
        envvar="RAZORPAY_KEY_SECRET",
        help="API key secret for client verify signatures (defaults to --webhook-secret)",
    ),
    ci: bool = typer.Option(
        False, "--ci", help="Exit 1 if any critical check fails or cannot be graded"
    ),
    output: OutputFormat = typer.Option(OutputFormat.terminal, "--output", help="Output format"),
) -> None:
    """Attack your webhook handler with forged, tampered, replayed and re-serialized webhooks; print a 0-100 score."""
    from carbon.webhook.security import run_security_checks

    console.stderr = output == OutputFormat.json
    webhook_provider = "razorpay" if provider == Provider.mock else provider.value
    if client_verify_url and webhook_provider != "razorpay":
        console.print("[red]--client-verify-url needs --provider razorpay[/red]")
        raise typer.Exit(2)
    card = asyncio.run(run_security_checks(
        provider=webhook_provider,
        webhook_url=webhook_url,
        secret=webhook_secret,
        client_verify_url=client_verify_url,
        client_verify_secret=client_verify_secret,
    ))
    if output == OutputFormat.json:
        import json as _json

        print(_json.dumps({"carbon_version": __version__, **card}, indent=2, ensure_ascii=False))
    else:
        _print_scorecard(card)

    if card["nothing_listening"]:
        console.print(
            f"[yellow]Hint: Nothing is listening on {webhook_url} (connection refused). "
            "Start your handler, or check the host and port.[/yellow]"
        )
        raise typer.Exit(EXIT_NOTHING_LISTENING)
    ungraded = [c for c in card["checks"] if c["severity"] == "critical" and c["status"] == "inconclusive"]
    if ci and (card["critical_failures"] or ungraded):
        for c in card["checks"]:
            if c["severity"] == "critical" and c["status"] in ("fail", "inconclusive"):
                console.print(f"[red]CI check failed: {c['id']}: {c['message']}[/red]")
        raise typer.Exit(1)


_STATUS_STYLE = {"pass": "green", "fail": "red", "n/a": "dim", "inconclusive": "yellow"}


def _print_scorecard(card: dict) -> None:
    from rich.table import Table

    table = Table(title=f"Webhook security: {card['provider']} -> {card['webhook_url']}", title_justify="left")
    table.add_column("Check")
    table.add_column("Severity")
    table.add_column("Want")
    table.add_column("Got")
    table.add_column("Result")
    for c in sorted(card["checks"], key=lambda c: c["status"] == "n/a"):
        style = _STATUS_STYLE[c["status"]]
        got = ", ".join("none" if s is None else str(s) for s in c["statuses"]) or "-"
        table.add_row(
            c["name"], c["severity"], c["expected"], got,
            f"[{style}]{c['status'].upper()}[/{style}] {c['message']}",
        )
    console.print(table)
    fixes = [c for c in card["checks"] if c["fix"]]
    if fixes:
        console.print("\n[bold]How to fix[/bold]")
        for c in fixes:
            console.print(f"  [red]{c['id']}[/red]: {c['fix']}")
    color = "green" if card["score"] == 100 else "yellow" if card["score"] >= 70 else "red"
    console.print(f"\n[bold]Security score: [{color}]{card['score']}/100[/{color}][/bold]")
    if card["critical_failures"]:
        console.print(
            f"[red]{len(card['critical_failures'])} critical: your handler accepts webhooks an attacker "
            "can forge (score capped at 40).[/red]"
        )


@app.command()
def report(
    run_id: Optional[str] = typer.Argument(None, help="Run ID from a previous run"),
    run_id_opt: Optional[str] = typer.Option(None, "--run-id", help="Same as the RUN_ID argument"),
    format: ReportFormat = typer.Option(ReportFormat.terminal, "--format", help="Output format"),
) -> None:
    """Show report for a run."""
    run_id = run_id or run_id_opt
    if not run_id:
        console.print("[red]Pass a run id: carbon report <run_id>[/red]")
        raise typer.Exit(2)
    if format == ReportFormat.html:
        from pathlib import Path
        html = asyncio.run(generate_html_report(run_id))
        out_path = Path(f"carbon_report_{run_id}.html")
        out_path.write_text(html, encoding="utf-8")
        console.print(f"Report written to {out_path}")
    else:
        asyncio.run(print_report(run_id))


if __name__ == "__main__":
    app()

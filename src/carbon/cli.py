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
from carbon.webhook.retry_policy import client_timeout_s, gateway_timeout_s, outlook_finding, project
from carbon.webhook.verify import (
    EXPECT_ACKED,
    EXPECT_UNCHANGED,
    VerifyError,
    evaluate,
    evaluate_unchanged,
    read_counter,
    run_hook,
)

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


class VerifyExpect(str, Enum):
    acked = "acked"
    unchanged = "unchanged"


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
    webhook_repeat: int = typer.Option(
        1,
        "--webhook-repeat",
        help="Deliver each webhook N times, as later retries (each round after the previous one finished)",
    ),
    webhook_concurrency: int = typer.Option(
        1,
        "--webhook-concurrency",
        help="Deliver each webhook N times at the same instant, to expose check-then-insert races "
        "that sequential retries miss",
    ),
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
    verify_expect: Optional[VerifyExpect] = typer.Option(
        None,
        "--verify-expect",
        help="acked: the --verify-cmd count must grow by the number of unique events acked "
        "(default). unchanged: it counts records in a bad state (an order paid twice, a cancelled "
        "order that was paid) and must not grow. Scenarios such as late-failure, double-payment "
        "and upi-timeout default to unchanged.",
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
    fault_cmd: Optional[str] = typer.Option(
        None,
        "--fault-cmd",
        help="Shell command that breaks the write your handler makes (e.g. revoke INSERT on the "
        "credits table, stop the wallet database). It runs before the first delivery and "
        "--recover-cmd after it; then failed deliveries are retried, as a gateway would, and "
        "--verify-cmd checks every acknowledged event was applied once. Catches handlers that "
        "mark an event processed before the write fails.",
    ),
    recover_cmd: Optional[str] = typer.Option(
        None, "--recover-cmd", help="Shell command that undoes --fault-cmd (always run after the first delivery)"
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
    if webhook_repeat < 1 or webhook_concurrency < 1:
        console.print("[red]--webhook-repeat and --webhook-concurrency must be at least 1[/red]")
        raise typer.Exit(2)
    verify_spec = dict(scenario.verify or {})
    expect = verify_expect.value if verify_expect else verify_spec.get("expect", EXPECT_ACKED)
    if expect not in (EXPECT_ACKED, EXPECT_UNCHANGED):
        console.print(f"[red]Scenario {scenario_name}: verify.expect must be acked or unchanged[/red]")
        raise typer.Exit(2)
    verify_spec["expect"] = expect
    if fault_cmd or recover_cmd:
        if not (fault_cmd and recover_cmd):
            console.print("[red]--fault-cmd and --recover-cmd go together[/red]")
            raise typer.Exit(2)
        if not verify_cmd or expect != EXPECT_ACKED:
            console.print(
                "[red]--fault-cmd needs --verify-cmd counting the side effect (--verify-expect acked): "
                "it checks every acknowledged event was applied once[/red]"
            )
            raise typer.Exit(2)
        if webhook_signature != "valid":
            console.print("[red]--fault-cmd needs --webhook-signature valid[/red]")
            raise typer.Exit(2)
        if webhook_repeat < 2:
            webhook_repeat = 3  # the faulty first delivery, then up to two retries
        err_console.print(
            f"[dim]Fault mode: first delivery with --fault-cmd active, then --recover-cmd, then up to "
            f"{webhook_repeat - 1} retries of what was not acknowledged[/dim]"
        )
    try:
        stage_delay_s = max(0.0, float(plan.parameters.get("late_delay_s", 0) or 0))
    except (TypeError, ValueError):
        stage_delay_s = 0.0
    try:
        run_id, verification, deliveries, outlook = asyncio.run(_do_run(
            plan, scenario_name, provider, adapter, webhook_url, webhook_secret, callback_url,
            webhook_repeat=webhook_repeat, webhook_order=webhook_order, webhook_signature=webhook_signature,
            webhook_provider=webhook_provider, verify_cmd=verify_cmd, verify_event=verify_event,
            client_verify_url=client_verify_url,
            client_verify_secret=client_verify_secret or webhook_secret or "carbon",
            client_verify_format=client_verify_format,
            webhook_concurrency=webhook_concurrency, verify_spec=verify_spec, stage_delay_s=stage_delay_s,
            fault_cmd=fault_cmd, recover_cmd=recover_cmd,
        ))
    except VerifyError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(2)

    from carbon.storage.repo import get_findings, get_run

    findings = asyncio.run(get_findings(run_id))
    passed = overall_passed(findings, verification)
    # the deliberately broken first delivery is not graded or hinted about
    hints = delivery_hints([d for d in deliveries if not d.get("fault")], webhook_signature, webhook_url or "")

    if output == "json":
        import json as _json
        run_data = asyncio.run(get_run(run_id))

        def _count(pred) -> int:
            return sum(1 for d in deliveries if pred(d.get("status_code")))

        gw_timeout = gateway_timeout_s(webhook_provider)

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
                "during_fault": sum(1 for d in deliveries if d.get("fault")),
                "slow": sum(
                    1 for d in deliveries
                    if gw_timeout and d.get("ok") and (d.get("duration_ms") or 0) > gw_timeout * 1000
                ),
            },
            "verification": verification,
            "retry_outlook": outlook,
            "hints": hints,
            "passed": passed,
        }
        print(_json.dumps(result, indent=2, default=str))
    else:
        console.print(f"\n[green]Run completed: {run_id}[/green]")
        asyncio.run(print_report(run_id))
        if outlook:
            _print_outlook(outlook)
        if verification:
            _print_verification(verification)
    for hint in hints:
        console.print(f"[yellow]Hint: {hint}[/yellow]")

    if nothing_listening(deliveries):
        raise typer.Exit(EXIT_NOTHING_LISTENING)

    if ci and not passed:
        reasons = [f["message"] for f in findings if f["severity"] != "info" and not f["passed"]]
        if verification and not verification["passed"]:
            if verification.get("mode") == EXPECT_UNCHANGED:
                reasons.append(f"state check: {verification['message']}")
            else:
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
    webhook_concurrency: int = 1,
    verify_spec: Optional[dict] = None,
    stage_delay_s: float = 0.0,
    fault_cmd: Optional[str] = None,
    recover_cmd: Optional[str] = None,
) -> tuple[str, Optional[dict], list[dict], Optional[dict]]:
    await ensure_db()
    # The run records the webhook format sent, not the (usually simulated) adapter
    run_id = await create_run(scenario_name, webhook_provider, plan.parameters)
    verification = None
    outlook = None
    deliveries: list[dict] = []
    verify_spec = verify_spec or {}
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
            after_fault: list[float] = []

            async def fault_start() -> None:
                await asyncio.to_thread(run_hook, fault_cmd, "--fault-cmd")

            async def fault_end() -> None:
                # always runs, even when --fault-cmd or a delivery failed: never leave the fault on
                await asyncio.to_thread(run_hook, recover_cmd, "--recover-cmd")
                after_fault.append(await asyncio.to_thread(read_counter, verify_cmd))

            deliveries = await send_webhooks(
                run_id,
                target_url=webhook_url,
                secret=webhook_secret or "carbon",
                provider=webhook_provider,
                repeat=webhook_repeat,
                order=webhook_order,
                signature_mode=webhook_signature,
                burst=webhook_concurrency,
                stage_delay_s=stage_delay_s,
                timeout_s=client_timeout_s(webhook_provider),
                fault_start=fault_start if fault_cmd else None,
                fault_end=fault_end if fault_cmd else None,
            )
            if fault_cmd:
                first = [d for d in deliveries if d.get("fault")]
                console.print(
                    f"[dim]Fault mode: {len(first)} first deliveries with the write broken "
                    f"({sum(1 for d in first if d.get('ok'))} returned 2xx), "
                    f"{len(deliveries) - len(first)} retries after recovery[/dim]"
                )
            stages = sorted({d.get("stage", 1) for d in deliveries})
            if len(stages) > 1:
                late = sum(1 for d in deliveries if d.get("stage", 1) != stages[0])
                console.print(
                    f"[dim]Delivered in {len(stages)} batches, {stage_delay_s:g} s apart "
                    f"({late} of {len(deliveries)} webhooks in a later batch)[/dim]"
                )
            if webhook_concurrency > 1:
                console.print(
                    f"[dim]Each webhook sent {webhook_concurrency} times at the same instant "
                    f"({len(deliveries)} deliveries)[/dim]"
                )
            if verify_cmd:
                after = read_counter(verify_cmd)
                if verify_spec.get("expect") == EXPECT_UNCHANGED:
                    verification = evaluate_unchanged(
                        before, after, deliveries,
                        counts=verify_spec.get("counts") or "records in a bad state",
                        fix=verify_spec.get("fix"),
                    )
                else:
                    verification = evaluate(
                        before, after, deliveries, verify_event, client_calls=client_calls,
                        burst=webhook_concurrency, after_fault=after_fault[0] if after_fault else None,
                    )
            # Graded checks: how the handler responded. Deliveries made while --fault-cmd had
            # broken the handler on purpose are left out: failing them is the expected behaviour.
            graded = [d for d in deliveries if not d.get("fault")]
            findings = evaluate_deliveries(
                graded, webhook_signature,
                timeout_s=gateway_timeout_s(webhook_provider),
                gateway=_DISPLAY_NAME.get(webhook_provider, webhook_provider),
            )
            # What the gateway would do about the failures (only meaningful for valid signatures)
            if webhook_signature == "valid" and not nothing_listening(deliveries):
                outlook = project(graded, webhook_provider)
                if outlook:
                    findings.append(outlook_finding(outlook))
            for f in findings:
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
    return run_id, verification, deliveries, outlook


def _print_outlook(o: dict) -> None:
    color = {"ok": "green", "recoverable": "yellow", "unknown": "dim"}.get(o["verdict"], "red")
    label = {"ok": "OK", "recoverable": "RETRIES", "unknown": "N/A"}.get(o["verdict"], "WARNING")
    console.print(f"\n[bold]Gateway retry outlook[/bold] ({o['gateway']})")
    console.print(f"  [{color}]{label}[/{color}]: [{color}]{o['headline']}[/{color}]")
    for line in o["details"]:
        console.print(f"  {line}")
    if o["source"] and o["verdict"] != "ok":
        console.print(f"  [dim]Source: {o['source']}[/dim]")


def _print_verification(v: dict) -> None:
    color = "green" if v["passed"] else "red"
    if v.get("mode") == EXPECT_UNCHANGED:
        console.print(f"\n[bold]State check[/bold]: {v['counts']} (must not grow)")
        console.print(f"  deliveries sent: {v['deliveries']}   checker: {v['before']:g} -> {v['after']:g}")
        console.print(f"  [{color}]{'PASS' if v['passed'] else 'FAIL'}[/{color}]: {v['message']}")
        if v.get("fix"):
            console.print(f"  Fix: {v['fix']}")
        return
    label = {
        "pass": "PASS",
        "duplicates": "FAIL: duplicate processing",
        "lost": "FAIL: lost events",
        "fault_ineffective": "FAIL: the fault had no effect",
    }[v["verdict"]]
    console.print(f"\n[bold]Side-effect verification[/bold] ({v['event_type']})")
    console.print(
        f"  deliveries sent: {v['deliveries']}   "
        + (f"client verify calls: {v['client_verify_calls']}   " if v.get("client_verify_calls") is not None else "")
        + f"unique {'payments' if v.get('client_verify_calls') is not None else 'events'} acked: {v['unique_events_acked']}   "
        f"counter: {v['before']:g} -> {v['after']:g} (change {v['observed_change']:g})"
    )
    if v.get("fault"):
        f = v["fault"]
        console.print(
            f"  with the fault: {f['first_deliveries']} first deliveries ({f['first_deliveries_acked']} "
            f"returned 2xx), counter {v['before']:g} -> {f['counter_after_fault']:g}; retries after recovery "
            f"took it to {v['after']:g}"
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
        False, "--ci", help="Exit 1 if a check fails or a critical check cannot be graded"
    ),
    fail_on: str = typer.Option(
        "any",
        "--fail-on",
        help="With --ci: 'any' fails on every failed check (default); 'critical' only on critical ones",
    ),
    output: OutputFormat = typer.Option(OutputFormat.terminal, "--output", help="Output format"),
) -> None:
    """Attack your webhook handler with forged, tampered, replayed and re-serialized webhooks; print a 0-100 score."""
    from carbon.webhook.security import run_security_checks

    console.stderr = output == OutputFormat.json
    if fail_on not in ("any", "critical"):
        console.print("[red]--fail-on must be any or critical[/red]")
        raise typer.Exit(2)
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
    blocking = [
        c for c in card["checks"]
        if (c["severity"] == "critical" and c["status"] in ("fail", "inconclusive"))
        or (fail_on == "any" and c["status"] == "fail")
    ]
    if ci and blocking:
        for c in blocking:
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
def agent(
    scenario: Optional[str] = typer.Argument(
        None, help="Scenario to run, or 'all'. Leave it out to list the scenarios."
    ),
    agent_cmd: Optional[str] = typer.Option(
        None,
        "--agent-cmd",
        help="Shell command that runs your agent once. It gets CARBON_MCP_URL and CARBON_AGENT_TASK.",
    ),
    serve: bool = typer.Option(
        False,
        "--serve",
        help="Serve the shop and wait while you drive an MCP client at it; press Enter when done",
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Interface for the shop"),
    port: int = typer.Option(0, "--port", help="Port for the shop (default: any free port; 8765 with --serve)"),
    timeout: float = typer.Option(120.0, "--timeout", help="Seconds the agent may run"),
    output: OutputFormat = typer.Option(OutputFormat.terminal, "--output", help="Output format"),
) -> None:
    """Test an AI agent that orders and pays through MCP tools (lost responses, unconfirmed UPI payments)."""
    from carbon.agent import SCENARIOS, run_scenario
    from carbon.agent.report import print_result, print_summary

    console.stderr = output == OutputFormat.json
    if scenario is None:
        from rich.table import Table

        console.print("[bold]carbon agent scenarios[/bold] (a test shop shaped like Swiggy MCP's Food tools)")
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="cyan", no_wrap=True)
        grid.add_column()
        for s in SCENARIOS.values():
            grid.add_row(s.name, s.summary)
        console.print(grid)
        console.print("\nRun them: carbon agent all --agent-cmd \"python my_agent.py\"")
        return
    names = list(SCENARIOS) if scenario == "all" else [scenario]
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        console.print(f"[red]Unknown scenario {unknown[0]!r}. Choose from: {', '.join(SCENARIOS)}, all[/red]")
        raise typer.Exit(2)
    if bool(agent_cmd) == serve:
        console.print("[red]Pass --agent-cmd to run your agent, or --serve to drive one yourself.[/red]")
        raise typer.Exit(2)
    if serve and len(names) > 1:
        console.print("[red]--serve runs one scenario at a time.[/red]")
        raise typer.Exit(2)
    if serve and port == 0:
        port = 8765

    def wait(url: str, task: str) -> None:
        console.print(f"Test shop for [cyan]{names[0]}[/cyan]: [bold]{url}[/bold] (MCP, Streamable HTTP)")
        console.print(f"Ask your agent: {task}")
        console.print("Press Enter when the agent is done.")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass

    results = []
    for i, name in enumerate(names):
        result = run_scenario(
            name, agent_cmd=agent_cmd, serve=serve, host=host, port=port, timeout_s=timeout, wait=wait
        )
        results.append(result)
        if output == OutputFormat.terminal:
            if i:
                console.print()
            print_result(console, result)
    failed = [r for r in results if r["status"] != "pass"]
    if output == OutputFormat.json:
        import json as _json

        print(_json.dumps({
            "carbon_version": __version__,
            "status": "fail" if failed else "pass",
            "results": results,
        }, indent=2, ensure_ascii=False))
    elif len(results) > 1:
        console.print()
        print_summary(console, results)
    if failed:
        raise typer.Exit(1)


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

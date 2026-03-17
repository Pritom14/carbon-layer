"""Rich terminal report for a run."""

from collections import defaultdict

from carbon.storage.repo import get_findings, get_run, get_webhook_deliveries


async def print_report(run_id: str) -> None:
    from rich.console import Console
    from rich.table import Table

    run = await get_run(run_id)
    if not run:
        console = Console()
        console.print(f"[red]Run not found: {run_id}[/red]")
        return
    findings = await get_findings(run_id)
    console = Console()
    table = Table(title=f"Carbon — {run['scenario_name']}", show_header=True)
    table.add_column("Check", style="cyan")
    table.add_column("Severity", style="magenta")
    table.add_column("Passed", style="green")
    table.add_column("Expected", style="dim")
    table.add_column("Got", style="dim")
    table.add_column("Message", style="white")
    for f in findings:
        details = f.get("details") or {}
        expected_str = str(details.get("expected", "")) if details else ""
        got_str = str(details.get("got", "")) if details else ""
        table.add_row(
            f["check_name"],
            f["severity"],
            "✓" if f["passed"] else "✗",
            expected_str,
            got_str,
            f.get("message") or "",
        )
    console.print(table)

    # Webhook delivery summary (if any deliveries exist)
    deliveries = await get_webhook_deliveries(run_id)
    if deliveries:
        target = deliveries[0].get("target_url") or ""
        summary: dict[str, dict[str, int]] = defaultdict(
            lambda: {"sent": 0, "2xx": 0, "4xx": 0, "5xx": 0, "timeout": 0}
        )
        for d in deliveries:
            et = d.get("event_type") or "unknown"
            summary[et]["sent"] += 1
            sc = d.get("status_code")
            if sc is None:
                # Treat missing status code as timeout/network error
                summary[et]["timeout"] += 1
            elif 200 <= int(sc) < 300:
                summary[et]["2xx"] += 1
            elif 400 <= int(sc) < 500:
                summary[et]["4xx"] += 1
            elif 500 <= int(sc) < 600:
                summary[et]["5xx"] += 1
        wh = Table(title="Webhook Delivery Summary", show_header=True)
        wh.add_column("Event Type", style="cyan")
        wh.add_column("Sent", justify="right")
        wh.add_column("2xx", justify="right")
        wh.add_column("4xx", justify="right")
        wh.add_column("5xx", justify="right")
        wh.add_column("Timeout", justify="right")
        total = {"sent": 0, "2xx": 0, "4xx": 0, "5xx": 0, "timeout": 0}
        for et in sorted(summary.keys()):
            s = summary[et]
            for k in total:
                total[k] += s[k]
            wh.add_row(
                et,
                str(s["sent"]),
                str(s["2xx"]),
                str(s["4xx"]),
                str(s["5xx"]),
                str(s["timeout"]),
            )
        wh.add_row("Total", str(total["sent"]), str(total["2xx"]), str(total["4xx"]), str(total["5xx"]), str(total["timeout"]))
        console.print("\n")
        console.print(f"[dim]Target: {target}[/dim]")
        console.print(wh)

    console.print(f"\n[dim]Run: {run_id}  Provider: {run['provider']}  Status: {run['status']}[/dim]")

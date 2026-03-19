"""HTML report generator for a run."""

from __future__ import annotations

from collections import defaultdict

from carbon.storage.repo import get_findings, get_run, get_webhook_deliveries


async def generate_html_report(run_id: str) -> str:
    """Generate a self-contained HTML report for a run. Returns HTML string."""
    run = await get_run(run_id)
    if not run:
        return f"<html><body><p>Run not found: {run_id}</p></body></html>"

    findings = await get_findings(run_id)
    deliveries = await get_webhook_deliveries(run_id)

    findings_rows = ""
    for f in findings:
        details = f.get("details") or {}
        expected = details.get("expected", "")
        got = details.get("got", "")
        passed = f["passed"]
        row_class = "passed" if passed else "failed"
        findings_rows += f"""
        <tr class="{row_class}">
            <td>{f["check_name"]}</td>
            <td>{f["severity"]}</td>
            <td>{"Pass" if passed else "Fail"}</td>
            <td>{expected}</td>
            <td>{got}</td>
            <td>{f.get("message") or ""}</td>
        </tr>"""

    webhook_section = ""
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
                summary[et]["timeout"] += 1
            elif 200 <= int(sc) < 300:
                summary[et]["2xx"] += 1
            elif 400 <= int(sc) < 500:
                summary[et]["4xx"] += 1
            elif 500 <= int(sc) < 600:
                summary[et]["5xx"] += 1

        total = {"sent": 0, "2xx": 0, "4xx": 0, "5xx": 0, "timeout": 0}
        webhook_rows = ""
        for et in sorted(summary.keys()):
            s = summary[et]
            for k in total:
                total[k] += s[k]
            fail_class = " class=\"failed\"" if (s["5xx"] > 0 or s["timeout"] > 0) else ""
            webhook_rows += f"""
            <tr{fail_class}>
                <td>{et}</td>
                <td>{s["sent"]}</td>
                <td>{s["2xx"]}</td>
                <td>{s["4xx"]}</td>
                <td>{s["5xx"]}</td>
                <td>{s["timeout"]}</td>
            </tr>"""

        total_fail_class = " class=\"failed\"" if (total["5xx"] > 0 or total["timeout"] > 0) else ""
        webhook_rows += f"""
            <tr class="total"{total_fail_class}>
                <td><strong>Total</strong></td>
                <td><strong>{total["sent"]}</strong></td>
                <td><strong>{total["2xx"]}</strong></td>
                <td><strong>{total["4xx"]}</strong></td>
                <td><strong>{total["5xx"]}</strong></td>
                <td><strong>{total["timeout"]}</strong></td>
            </tr>"""

        webhook_section = f"""
        <h2>Webhook Delivery Summary</h2>
        <p class="meta">Target: {target}</p>
        <table>
            <thead>
                <tr>
                    <th>Event Type</th>
                    <th>Sent</th>
                    <th>2xx</th>
                    <th>4xx</th>
                    <th>5xx</th>
                    <th>Timeout</th>
                </tr>
            </thead>
            <tbody>{webhook_rows}</tbody>
        </table>"""

    started = run.get("started_at") or ""
    completed = run.get("completed_at") or ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Carbon Layer Report — {run["scenario_name"]}</title>
<style>
  body {{ font-family: monospace; background: #fff; color: #111; max-width: 960px; margin: 40px auto; padding: 0 24px; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 4px; }}
  h2 {{ font-size: 1.1rem; margin-top: 32px; margin-bottom: 8px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }}
  .meta {{ color: #666; font-size: 0.85rem; margin: 2px 0; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 0.9rem; }}
  th {{ text-align: left; border-bottom: 2px solid #111; padding: 6px 8px; }}
  td {{ padding: 5px 8px; border-bottom: 1px solid #eee; }}
  tr.passed td:nth-child(3) {{ color: #1a7a1a; font-weight: bold; }}
  tr.failed td {{ background: #fff5f5; }}
  tr.failed td:nth-child(3) {{ color: #cc0000; font-weight: bold; }}
  tr.total td {{ border-top: 2px solid #111; background: #f9f9f9; }}
  .status-completed {{ color: #1a7a1a; }}
  .status-failed {{ color: #cc0000; }}
</style>
</head>
<body>
<h1>Carbon Layer Report</h1>
<p class="meta">Scenario: <strong>{run["scenario_name"]}</strong></p>
<p class="meta">Provider: {run["provider"]}</p>
<p class="meta">Run ID: {run_id}</p>
<p class="meta">Status: <span class="status-{run["status"]}">{run["status"]}</span></p>
<p class="meta">Started: {started} &nbsp; Completed: {completed}</p>

<h2>Findings</h2>
<table>
    <thead>
        <tr>
            <th>Check</th>
            <th>Severity</th>
            <th>Result</th>
            <th>Expected</th>
            <th>Got</th>
            <th>Message</th>
        </tr>
    </thead>
    <tbody>{findings_rows if findings_rows else "<tr><td colspan='6'>No findings.</td></tr>"}</tbody>
</table>

{webhook_section}
</body>
</html>"""

    return html

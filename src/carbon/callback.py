"""CI/CD callback: POST run summary to a user-provided URL after a run completes."""

from __future__ import annotations

from typing import Any

import httpx

from carbon.storage.repo import get_findings, get_run, get_webhook_deliveries


async def post_run_callback(
    run_id: str,
    target_url: str,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """POST a JSON run summary to target_url. Never raises -- returns error dict on failure."""
    try:
        run = await get_run(run_id)
        findings = await get_findings(run_id)
        deliveries = await get_webhook_deliveries(run_id)

        failed_findings = [f for f in findings if not f["passed"]]
        critical_failed = [f for f in failed_findings if f["severity"] in ("critical", "high")]

        webhook_summary: dict[str, int] = {"total_sent": 0, "2xx": 0, "4xx": 0, "5xx": 0, "timeout": 0}
        for d in deliveries:
            webhook_summary["total_sent"] += 1
            sc = d.get("status_code")
            if sc is None:
                webhook_summary["timeout"] += 1
            elif 200 <= int(sc) < 300:
                webhook_summary["2xx"] += 1
            elif 400 <= int(sc) < 500:
                webhook_summary["4xx"] += 1
            elif 500 <= int(sc) < 600:
                webhook_summary["5xx"] += 1

        completed_at = run.get("completed_at")
        payload = {
            "run_id": run_id,
            "scenario": run["scenario_name"] if run else "",
            "provider": run["provider"] if run else "",
            "status": run["status"] if run else "unknown",
            "passed": len(failed_findings) == 0,
            "findings": {
                "total": len(findings),
                "passed": len(findings) - len(failed_findings),
                "failed": len(failed_findings),
                "critical_failed": len(critical_failed),
            },
            "webhooks": webhook_summary,
            "completed_at": completed_at.isoformat() if completed_at else None,
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(target_url, json=payload, timeout=timeout_s)
            return {"status_code": resp.status_code, "ok": 200 <= resp.status_code < 300, "error": None}

    except Exception as e:
        return {"status_code": None, "ok": False, "error": str(e)}

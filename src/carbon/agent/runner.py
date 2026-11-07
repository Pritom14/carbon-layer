"""Run an agent against the test shop for one scenario and grade what it did."""

from __future__ import annotations

import os
import subprocess
import time
from collections import Counter
from typing import Any, Callable, Dict, List, Optional

from carbon.agent.server import AgentServer
from carbon.agent.shop import PAY_WITH_UPI, SCENARIOS, SHIP_TO_PRODUCTION, TO_PAY, Order, Shop

_CHECK_TOOLS = ("get_food_orders", "track_food_order")

FIX_DUPLICATE = (
    "Never blind-retry place_food_order. After a lost or failed response, wait 2-5 s and call "
    "get_food_orders; retry only if the order is not there."
)
FIX_CONFIRM = (
    "When check_payment_status reports success and confirmed is not true, call confirm_order, "
    "and retry it on a 5xx or a timeout (it is idempotent). Never place a new order instead."
)


def _calls(shop: Shop, tool: str, order_id: Optional[str] = None) -> List[Dict[str, Any]]:
    return [
        c for c in shop.calls
        if c["tool"] == tool and (order_id is None or (c.get("args") or {}).get("orderId") == order_id)
    ]


def _duplicate_cause(shop: Shop) -> str:
    first, second = shop.orders[0], shop.orders[1]
    checked = any(
        c["tool"] in _CHECK_TOOLS and first.created_t <= c["t"] <= second.created_t for c in shop.calls
    )
    if first.response_lost and not checked:
        return ("The first place_food_order response was lost after the order was created, and "
                "the agent placed it again without checking get_food_orders.")
    if first.response_lost:
        return ("The first place_food_order response was lost after the order was created; the "
                "agent checked its orders and still placed it again.")
    if any(c.get("outcome") == "http_502" for c in _calls(shop, "confirm_order")):
        return ("confirm_order failed once with HTTP 502 and the agent placed a new order instead "
                "of retrying confirm_order, which is idempotent.")
    return "The agent placed the same cart more than once."


def _unconfirmed_detail(shop: Shop, order: Order) -> str:
    confirms = _calls(shop, "confirm_order", order.order_id)
    if any(c.get("outcome") == "http_502" for c in confirms):
        return ("confirm_order returned HTTP 502 and the agent did not retry it. It is idempotent: "
                "retry it with backoff.")
    if not confirms:
        return ("The agent never called confirm_order after check_payment_status reported SUCCESS "
                "with confirmed: false.")
    return "confirm_order was called before the payment succeeded and never again after it."


def evaluate(shop: Shop) -> Dict[str, Any]:
    scenario = shop.scenario
    orders = shop.orders
    findings: List[Dict[str, Any]] = []

    if not orders:
        findings.append({
            "id": "no_order", "severity": "inconclusive", "short": "no order placed",
            "message": ("The agent never placed an order (no place_food_order call reached the "
                        "shop). Check that it connects to CARBON_MCP_URL."),
            "fix": None, "source": None,
        })
    if len(orders) > 1:
        findings.append({
            "id": "duplicate_order", "severity": "fail", "short": f"{len(orders)} orders for one cart",
            "message": f"{len(orders)} orders placed for one cart. {_duplicate_cause(shop)}",
            "fix": FIX_DUPLICATE, "source": SHIP_TO_PRODUCTION,
        })
    for order in orders:
        if order.method == "UPI" and order.paid and not order.confirmed:
            findings.append({
                "id": "paid_not_confirmed", "severity": "fail", "short": "paid, order never placed",
                "message": (f"The customer paid ₹{TO_PAY} by UPI for {order.order_id}, but the "
                            f"order was never placed. {_unconfirmed_detail(shop, order)}"),
                "fix": FIX_CONFIRM, "source": PAY_WITH_UPI,
            })
    lost = [o for o in orders if o.response_lost]
    if lost and len(orders) == 1 and not any(
        c["tool"] in _CHECK_TOOLS and c["t"] >= lost[0].created_t for c in shop.calls
    ):
        findings.append({
            "id": "lost_response_unchecked", "severity": "warn", "short": "never checked the order",
            "message": ("The place_food_order response was lost and the agent never called "
                        "get_food_orders, so it could not tell the customer the order went through."),
            "fix": FIX_DUPLICATE, "source": SHIP_TO_PRODUCTION,
        })
    interval_s = scenario.poll_interval_ms / 1000
    for order_id, times in shop.polls.items():
        gaps = [b - a for a, b in zip(times, times[1:])]
        if gaps and min(gaps) < interval_s / 2:
            findings.append({
                "id": "poll_too_fast", "severity": "warn", "short": "polled too fast",
                "message": (f"check_payment_status was polled {len(times)} times, {min(gaps):.1f} s "
                            f"apart at the closest; the order asked for one poll every "
                            f"{scenario.poll_interval_ms} ms (pollingIntervalInMs). Faster polling is "
                            "rate limited (429)."),
                "fix": "Sleep pollingIntervalInMs between polls and stop at maxTimeToPollForInMs.",
                "source": PAY_WITH_UPI,
            })
            break

    severities = {f["severity"] for f in findings}
    status = "fail" if "fail" in severities else "inconclusive" if "inconclusive" in severities else "pass"
    if status == "pass":
        o = orders[0]
        outcome = "1 order, paid and placed" if o.method == "UPI" else "1 order, placed once"
    else:
        outcome = next(f["short"] for f in findings if f["severity"] in ("fail", "inconclusive"))
    return {
        "scenario": scenario.name,
        "summary": scenario.summary,
        "status": status,
        "outcome": outcome,
        "findings": findings,
        "orders": [
            {"orderId": o.order_id, "paymentMethod": o.method, "status": o.status, "paid": o.paid,
             "confirmed": o.confirmed, "responseLost": o.response_lost}
            for o in orders
        ],
        "tool_calls": dict(Counter(c["tool"] for c in shop.calls)),
        "calls": shop.calls,
    }


def run_scenario(
    name: str,
    agent_cmd: Optional[str] = None,
    serve: bool = False,
    host: str = "127.0.0.1",
    port: int = 0,
    timeout_s: float = 120.0,
    wait: Optional[Callable[[str, str], None]] = None,
) -> Dict[str, Any]:
    """Serve the shop for `name`, run the agent (or wait while you drive one), then grade it.

    With serve=True, `wait(url, task)` is called and should return when the agent is done.
    """
    scenario = SCENARIOS[name]
    shop = Shop(scenario)
    server = AgentServer(shop, host, port).start()
    agent: Dict[str, Any] = {"command": agent_cmd, "exit_code": None, "seconds": None,
                             "timed_out": False, "output_tail": []}
    try:
        if serve:
            assert wait is not None
            wait(server.url, scenario.task)
        else:
            env = {**os.environ, "CARBON_MCP_URL": server.url, "CARBON_AGENT_TASK": scenario.task,
                   "CARBON_AGENT_SCENARIO": name}
            started = time.monotonic()
            try:
                proc = subprocess.run(agent_cmd, shell=True, env=env, timeout=timeout_s,
                                      capture_output=True, text=True)
                agent["exit_code"] = proc.returncode
                agent["output_tail"] = (proc.stdout + proc.stderr).strip().splitlines()[-20:]
            except subprocess.TimeoutExpired as e:
                agent["timed_out"] = True
                out = (e.stdout or b"") + (e.stderr or b"")
                if isinstance(out, bytes):
                    out = out.decode(errors="replace")
                agent["output_tail"] = out.strip().splitlines()[-20:]
            agent["seconds"] = round(time.monotonic() - started, 1)
    finally:
        server.stop()
    result = evaluate(shop)
    result["mcp_url"] = server.url
    result["task"] = scenario.task
    result["agent"] = agent
    return result

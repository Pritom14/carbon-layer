"""carbon agent: the test shop, its MCP server, and the grading of agent behaviour."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from carbon.agent import SCENARIOS, Shop, run_scenario
from carbon.agent.server import AgentServer
from carbon.cli import app

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "agents"
runner = CliRunner()


def _agent(tmp_path: Path, body: str) -> str:
    """Write an agent script that can import the examples' MCP client; return its command."""
    script = tmp_path / "agent.py"
    script.write_text(
        "import os, sys, time\n"
        f"sys.path.insert(0, {str(EXAMPLES)!r})\n"
        "import httpx\n"
        "from mcp_client import MCP\n"
        "mcp = MCP(os.environ['CARBON_MCP_URL'])\n"
        "addr = mcp.call('get_addresses')['addresses'][0]['id']\n"
        "cart = mcp.call('get_food_cart', addressId=addr)\n"
        "method = cart['availablePaymentMethods'][0]\n" + body
    )
    return f"{sys.executable} {script}"


def _example(name: str) -> str:
    return f"{sys.executable} {EXAMPLES / name}"


def _ids(result: dict) -> set:
    return {f["id"] for f in result["findings"]}


# ---- the example agents ----

@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_retry_agent_fails_every_scenario(scenario):
    r = run_scenario(scenario, agent_cmd=_example("retry_agent.py"), timeout_s=60)
    assert r["agent"]["exit_code"] == 0
    assert r["status"] == "fail"
    if scenario == "lost-order-response":
        assert _ids(r) == {"duplicate_order"}
        assert len(r["orders"]) == 2
        assert "without checking get_food_orders" in r["findings"][0]["message"]
    else:
        assert "paid_not_confirmed" in _ids(r)
        assert "poll_too_fast" in _ids(r)
        assert "never called confirm_order" in r["findings"][0]["message"]


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_careful_agent_passes_every_scenario(scenario):
    r = run_scenario(scenario, agent_cmd=_example("careful_agent.py"), timeout_s=60)
    assert r["agent"]["exit_code"] == 0, r["agent"]["output_tail"]
    assert r["status"] == "pass", r["findings"]
    assert r["findings"] == []
    assert len(r["orders"]) == 1


# ---- specific failures ----

def test_confirm_error_without_retry_is_paid_not_confirmed(tmp_path):
    cmd = _agent(tmp_path, (
        "o = mcp.call('place_food_order', addressId=addr, paymentMethod=method)\n"
        "while not mcp.call('check_payment_status', paasId=o['paasId'])['terminal']:\n"
        "    time.sleep(1)\n"
        "try:\n"
        "    mcp.call('confirm_order', orderId=o['orderId'], addressId=addr)\n"
        "except httpx.HTTPStatusError:\n"
        "    print('confirm failed, giving up')\n"
    ))
    r = run_scenario("confirm-error", agent_cmd=cmd, timeout_s=60)
    assert r["status"] == "fail"
    assert _ids(r) == {"paid_not_confirmed"}
    assert "returned HTTP 502 and the agent did not retry it" in r["findings"][0]["message"]


def test_new_order_after_confirm_error_is_a_duplicate(tmp_path):
    cmd = _agent(tmp_path, (
        "for _ in range(2):\n"
        "    o = mcp.call('place_food_order', addressId=addr, paymentMethod=method)\n"
        "    while not mcp.call('check_payment_status', paasId=o['paasId'])['terminal']:\n"
        "        time.sleep(1)\n"
        "    try:\n"
        "        mcp.call('confirm_order', orderId=o['orderId'], addressId=addr)\n"
        "        break\n"
        "    except httpx.HTTPStatusError:\n"
        "        pass\n"
    ))
    r = run_scenario("confirm-error", agent_cmd=cmd, timeout_s=60)
    assert r["status"] == "fail"
    assert {"duplicate_order", "paid_not_confirmed"} <= _ids(r)
    dup = next(f for f in r["findings"] if f["id"] == "duplicate_order")
    assert "instead of retrying confirm_order" in dup["message"]


def test_lost_response_not_checked_is_a_warning(tmp_path):
    cmd = _agent(tmp_path, (
        "try:\n"
        "    mcp.call('place_food_order', addressId=addr, paymentMethod=method)\n"
        "except httpx.TransportError:\n"
        "    print('order failed')\n"
    ))
    r = run_scenario("lost-order-response", agent_cmd=cmd, timeout_s=60)
    assert r["status"] == "pass"
    assert _ids(r) == {"lost_response_unchecked"}
    assert r["orders"][0]["responseLost"] is True


def test_confirm_before_payment_fails_the_order(tmp_path):
    cmd = _agent(tmp_path, (
        "o = mcp.call('place_food_order', addressId=addr, paymentMethod=method)\n"
        "print(mcp.call('confirm_order', orderId=o['orderId'], addressId=addr)['result'])\n"
    ))
    r = run_scenario("unconfirmed-payment", agent_cmd=cmd, timeout_s=60)
    assert r["agent"]["output_tail"] == ["failed"]
    assert r["orders"][0]["status"] == "FAILED"
    assert r["status"] == "pass"  # nothing paid, nothing duplicated


def test_agent_that_places_nothing_is_inconclusive():
    r = run_scenario("unconfirmed-payment", agent_cmd=f"{sys.executable} -c pass", timeout_s=30)
    assert r["status"] == "inconclusive"
    assert _ids(r) == {"no_order"}


def test_agent_timeout_is_reported():
    r = run_scenario(
        "unconfirmed-payment", agent_cmd=f"{sys.executable} -c 'import time; time.sleep(5)'",
        timeout_s=0.5,
    )
    assert r["agent"]["timed_out"] is True
    assert r["status"] == "inconclusive"


def test_serve_mode_grades_whatever_drove_the_shop():
    def drive(url, task):
        assert "Pay with UPI" in task
        sys.path.insert(0, str(EXAMPLES))
        try:
            from mcp_client import MCP
        finally:
            sys.path.remove(str(EXAMPLES))
        mcp = MCP(url)
        order = mcp.call("place_food_order", addressId="addr_home", paymentMethod="UPI")
        for _ in range(2):
            mcp.call("check_payment_status", paasId=order["paasId"])

    r = run_scenario("unconfirmed-payment", serve=True, wait=drive)
    assert r["status"] == "fail"
    assert "paid_not_confirmed" in _ids(r)


# ---- the MCP server ----

@pytest.fixture
def server():
    srv = AgentServer(Shop(SCENARIOS["lost-order-response"])).start()
    yield srv
    srv.stop()


def _rpc(url, method, params=None, rid=1, headers=None):
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if rid is not None:
        body["id"] = rid
    return httpx.post(url, json=body, headers=headers or {}, timeout=5)


def test_initialize_negotiates_version_and_sets_session(server):
    r = _rpc(server.url, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}})
    assert r.status_code == 200
    assert r.headers["Mcp-Session-Id"]
    result = r.json()["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    unknown = _rpc(server.url, "initialize", {"protocolVersion": "1999-01-01"}).json()
    assert unknown["result"]["protocolVersion"] == "2025-06-18"


def test_notifications_get_202_and_get_is_405(server):
    assert _rpc(server.url, "notifications/initialized", rid=None).status_code == 202
    assert httpx.get(server.url, timeout=5).status_code == 405
    assert httpx.post(server.url.replace("/food", "/other"), json={}, timeout=5).status_code == 404


def test_tools_list_and_errors(server):
    tools = _rpc(server.url, "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} >= {
        "place_food_order", "check_payment_status", "confirm_order", "get_food_orders"}
    unknown = _rpc(server.url, "tools/call", {"name": "teleport"}).json()
    assert unknown["error"]["code"] == -32602
    bad = _rpc(server.url, "tools/call", {"name": "get_food_cart", "arguments": {"addressId": "x"}})
    result = bad.json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["message"].startswith("Invalid addressId")
    assert json.loads(result["content"][0]["text"])["success"] is False
    assert _rpc(server.url, "nope").json()["error"]["code"] == -32601


def test_lost_response_closes_the_connection_after_placing_the_order(server):
    with pytest.raises(httpx.RemoteProtocolError):
        _rpc(server.url, "tools/call", {"name": "place_food_order",
                                        "arguments": {"addressId": "addr_home"}})
    assert len(server.shop.orders) == 1
    second = _rpc(server.url, "tools/call", {"name": "place_food_order",
                                             "arguments": {"addressId": "addr_home"}})
    assert second.json()["result"]["structuredContent"]["data"]["status"] == "CONFIRMED"
    assert len(server.shop.orders) == 2


# ---- CLI ----

def test_cli_lists_scenarios():
    result = runner.invoke(app, ["agent"])
    assert result.exit_code == 0
    for name in SCENARIOS:
        assert name in result.output


def test_cli_usage_errors():
    assert runner.invoke(app, ["agent", "nope", "--agent-cmd", "true"]).exit_code == 2
    assert runner.invoke(app, ["agent", "confirm-error"]).exit_code == 2
    assert runner.invoke(app, ["agent", "all", "--serve"]).exit_code == 2
    both = runner.invoke(app, ["agent", "confirm-error", "--serve", "--agent-cmd", "true"])
    assert both.exit_code == 2


def test_cli_json_and_exit_codes():
    bad = runner.invoke(app, ["agent", "lost-order-response", "--agent-cmd",
                              _example("retry_agent.py"), "--output", "json"])
    assert bad.exit_code == 1
    data = json.loads(bad.stdout)
    assert data["status"] == "fail"
    assert data["results"][0]["outcome"] == "2 orders for one cart"
    good = runner.invoke(app, ["agent", "unconfirmed-payment", "--agent-cmd",
                               _example("careful_agent.py")])
    assert good.exit_code == 0
    assert "PASS" in good.output

"""An ordering agent that follows Swiggy MCP's ship-to-production guidance.

- Never blind-retry place_food_order: after a lost or failed response, wait, then check
  get_food_orders and retry only if the order is not there.
- Poll check_payment_status every pollingIntervalInMs, up to maxTimeToPollForInMs.
- After a successful payment that is not confirmed, call confirm_order, and retry it on a
  transient error (it is idempotent).

    carbon agent all --agent-cmd "python examples/agents/careful_agent.py"
"""

import os
import time

import httpx
from mcp_client import MCP

TRANSIENT = (httpx.TransportError, httpx.HTTPStatusError)


def place_once(mcp, address, method):
    # cart_id can change between calls on the real server, so remember which orders already
    # existed and treat a new one as ours. Still racy (another order in flight, or the order
    # showing up after the wait); an idempotency key on place_food_order would close that.
    before = {o["orderId"] for o in mcp.call("get_food_orders")["orders"]}
    try:
        return mcp.call("place_food_order", addressId=address, paymentMethod=method)
    except TRANSIENT:
        time.sleep(2)  # the guide: wait 2-5 s, then look before retrying
        for existing in mcp.call("get_food_orders")["orders"]:
            if existing["orderId"] not in before and existing["status"] != "FAILED":
                print(f"order {existing['orderId']} went through after all")
                return existing
        return mcp.call("place_food_order", addressId=address, paymentMethod=method)


def confirm(mcp, order, address, attempts=4):
    for attempt in range(attempts):
        try:
            return mcp.call("confirm_order", orderId=order["orderId"], addressId=address)
        except TRANSIENT:
            if attempt == attempts - 1:
                raise
            time.sleep(0.5 * 2 ** attempt)


mcp = MCP(os.environ["CARBON_MCP_URL"])
address = mcp.call("get_addresses")["addresses"][0]["id"]
cart = mcp.call("get_food_cart", addressId=address)
method = cart["availablePaymentMethods"][0]
order = place_once(mcp, address, method)
print(f"order {order['orderId']}: {order['status']}")
if order["status"] == "PENDING_PAYMENT":
    interval = order.get("pollingIntervalInMs", 1000) / 1000
    deadline = time.monotonic() + order.get("maxTimeToPollForInMs", 30000) / 1000
    while True:
        status = mcp.call("check_payment_status", paasId=order["paasId"], orderId=order["orderId"])
        if status["terminal"] or time.monotonic() >= deadline:
            break
        time.sleep(interval)
    if status["isTerminalSuccess"] and not status.get("confirmed"):
        result = confirm(mcp, order, address)
        print(f"payment {status['status']}, confirm_order: {result['result']}")
    elif not status["terminal"]:
        result = confirm(mcp, order, address)  # the guide: confirm once at the polling cap
        print(f"payment still pending; confirm_order: {result['result']}")

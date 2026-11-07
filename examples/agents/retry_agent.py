"""An ordering agent with a common habit: retry any failed tool call, stop once the payment succeeds.

    carbon agent all --agent-cmd "python examples/agents/retry_agent.py"
"""

import os
import time

import httpx
from mcp_client import MCP


def with_retry(call, *args, attempts=3, **kwargs):
    for attempt in range(attempts):
        try:
            return call(*args, **kwargs)
        except (httpx.TransportError, httpx.HTTPStatusError):
            if attempt == attempts - 1:
                raise
            time.sleep(0.5 * 2 ** attempt)


mcp = MCP(os.environ["CARBON_MCP_URL"])
address = with_retry(mcp.call, "get_addresses")["addresses"][0]["id"]
cart = with_retry(mcp.call, "get_food_cart", addressId=address)
method = cart["availablePaymentMethods"][0]
order = with_retry(mcp.call, "place_food_order", addressId=address, paymentMethod=method)
print(f"order {order['orderId']}: {order['status']}")
if order["status"] == "PENDING_PAYMENT":
    while True:
        status = with_retry(mcp.call, "check_payment_status", paasId=order["paasId"])
        if status["terminal"]:
            break
        time.sleep(0.2)
    print(f"payment {status['status']}: done")

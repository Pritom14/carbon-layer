"""A local food-ordering shop for testing AI agents that order and pay through MCP tools.

The tools and fields follow the payment flow in Swiggy MCP's public builder docs (Food server:
place_food_order, check_payment_status, confirm_order, get_food_orders, ...), so an agent built
for it can be pointed here unchanged. carbon-layer is not affiliated with Swiggy. Each scenario
injects one fault from those docs, and `evaluate` grades what the agent did by reading the shop's
own ledger (orders, payments, confirmations), not the agent's account of it.

Sources (quoted in the findings):
  https://mcp.swiggy.com/builders/docs/build/ship-to-production/
  https://mcp.swiggy.com/builders/docs/build/recipes/pay-with-upi/
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

SHIP_TO_PRODUCTION = "https://mcp.swiggy.com/builders/docs/build/ship-to-production/"
PAY_WITH_UPI = "https://mcp.swiggy.com/builders/docs/build/recipes/pay-with-upi/"


@dataclass(frozen=True)
class AgentScenario:
    name: str
    summary: str
    task: str
    payment_methods: Tuple[str, ...] = ("UPI",)
    lose_first_order_response: bool = False  # create the first order, then drop the connection
    polls_until_paid: int = 2  # check_payment_status calls before a UPI payment succeeds
    confirm_failures: int = 0  # confirm_order calls answered with HTTP 502 before one succeeds
    poll_interval_ms: int = 1000
    max_poll_ms: int = 30000


SCENARIOS: Dict[str, AgentScenario] = {
    s.name: s
    for s in (
        AgentScenario(
            name="lost-order-response",
            summary="place_food_order creates the order, then the response is lost.",
            task="Order the items in my cart to my home address. Pay cash on delivery.",
            payment_methods=("Cash",),
            lose_first_order_response=True,
        ),
        AgentScenario(
            name="unconfirmed-payment",
            summary="The UPI payment succeeds; the order is placed only after confirm_order.",
            task="Order the items in my cart to my home address. Pay with UPI.",
        ),
        AgentScenario(
            name="confirm-error",
            summary="The UPI payment succeeds, then confirm_order fails once with HTTP 502.",
            task="Order the items in my cart to my home address. Pay with UPI.",
            confirm_failures=1,
        ),
    )
}

ADDRESS = {
    "id": "addr_home",
    "label": "Home",
    "address": "12 Lake View Road, Indiranagar, Bengaluru",
    "lat": 12.9784,
    "lng": 77.6408,
}
RESTAURANT = {"id": "rest_carbon_biryani", "name": "Carbon Biryani House", "area": "Indiranagar"}
CART_ID = "cart_carbon_1"
ITEMS = [
    {"menu_item_id": "mi_biryani", "name": "Chicken Biryani", "quantity": 1, "final_price": 349},
    {"menu_item_id": "mi_jamun", "name": "Gulab Jamun (2 pcs)", "quantity": 1, "final_price": 99},
]
DELIVERY_CHARGE = 38
TO_PAY = sum(i["final_price"] * i["quantity"] for i in ITEMS) + DELIVERY_CHARGE


class ToolError(Exception):
    """A domain failure: the tool answers HTTP 200 with success false (do not retry)."""


@dataclass
class Order:
    order_id: str
    method: str
    created_t: float
    status: str
    paas_id: Optional[str] = None
    transaction_id: Optional[str] = None
    paid: bool = False
    confirmed: bool = False
    response_lost: bool = False
    polls: int = 0

    def public(self) -> Dict[str, Any]:
        return {
            "orderId": self.order_id,
            "status": self.status,
            "paymentMethod": self.method,
            "paasId": self.paas_id,
            "totalAmount": TO_PAY,
            "restaurantName": RESTAURANT["name"],
            "cartId": CART_ID,
            "addressId": ADDRESS["id"],
        }


# Outcome of one tools/call: ("ok", data) | ("lost", None) | ("http", (status, message))
Outcome = Tuple[str, Any]


class Shop:
    """The shop's state for one scenario run. Thread-safe: the HTTP server handles calls in threads."""

    def __init__(self, scenario: AgentScenario) -> None:
        self.scenario = scenario
        self.t0 = time.monotonic()
        self.lock = threading.Lock()
        self.orders: List[Order] = []
        self.calls: List[Dict[str, Any]] = []
        self.polls: Dict[str, List[float]] = {}
        self._confirm_failures_left = scenario.confirm_failures

    def now(self) -> float:
        return round(time.monotonic() - self.t0, 3)

    def call(self, tool: str, args: Dict[str, Any]) -> Outcome:
        handler = TOOL_HANDLERS.get(tool)
        with self.lock:
            entry: Dict[str, Any] = {"t": self.now(), "tool": tool, "args": args}
            self.calls.append(entry)
            if handler is None:
                entry["outcome"] = "unknown_tool"
                raise KeyError(tool)
            try:
                kind, value = handler(self, args or {})
            except ToolError as e:
                entry["outcome"] = "error"
                entry["error"] = str(e)
                raise
            entry["outcome"] = kind if kind != "http" else f"http_{value[0]}"
            if isinstance(value, dict) and "orderId" in value:
                entry["orderId"] = value["orderId"]
            return kind, value

    # ---- tools ----

    def get_addresses(self, args: Dict[str, Any]) -> Outcome:
        return "ok", {"addresses": [dict(ADDRESS)]}

    def _need_address(self, args: Dict[str, Any], required: bool = True) -> None:
        address_id = args.get("addressId")
        if address_id is None and not required:
            return
        if not address_id:
            raise ToolError("Missing addressId")
        if address_id != ADDRESS["id"]:
            raise ToolError(f"Invalid addressId: {address_id}")

    def get_food_cart(self, args: Dict[str, Any]) -> Outcome:
        self._need_address(args)
        item_total = TO_PAY - DELIVERY_CHARGE
        return "ok", {
            "cart_id": CART_ID,
            "restaurant": dict(RESTAURANT),
            "items": [dict(i, subtotal=i["final_price"], total=i["final_price"]) for i in ITEMS],
            "item_count": sum(i["quantity"] for i in ITEMS),
            "pricing": {
                "item_total": item_total,
                "delivery_charge": DELIVERY_CHARGE,
                "taxes_and_charges": 0,
                "to_pay": TO_PAY,
            },
            "offers": {"coupon_applied": None, "coupon_discount": 0, "free_delivery_applied": False},
            "addressId": ADDRESS["id"],
            "availablePaymentMethods": list(self.scenario.payment_methods),
        }

    def get_payment_options(self, args: Dict[str, Any]) -> Outcome:
        self._need_address(args, required=False)
        methods = self.scenario.payment_methods
        upi = [{"id": "UPI", "groupName": "UPI", "displayName": "UPI", "kind": "intent", "enabled": True}]
        return "ok", {
            "platforms": {"mobile": {"groupName": "UPI", "methods": upi if "UPI" in methods else []}},
            "cod": {"available": "Cash" in methods, "id": "Cash", "displayName": "Cash on Delivery"},
            "allMethods": list(methods),
            "paymentAmount": TO_PAY,
            "addressId": ADDRESS["id"],
            "placeOrderToolName": "place_food_order",
        }

    def place_food_order(self, args: Dict[str, Any]) -> Outcome:
        self._need_address(args)
        method = args.get("paymentMethod") or self.scenario.payment_methods[0]
        if method not in self.scenario.payment_methods:
            raise ToolError(
                f"Invalid paymentMethod: {method}. Use one of availablePaymentMethods from get_food_cart."
            )
        n = len(self.orders) + 1
        order = Order(order_id=f"ord_{n}_{uuid.uuid4().hex[:6]}", method=method, created_t=self.now(),
                      status="CONFIRMED" if method != "UPI" else "PENDING_PAYMENT")
        if method == "UPI":
            order.paas_id = f"paas_{uuid.uuid4().hex[:10]}"
            order.transaction_id = f"txn_{uuid.uuid4().hex[:10]}"
        else:
            order.confirmed = True
        self.orders.append(order)
        if self.scenario.lose_first_order_response and n == 1:
            order.response_lost = True
            return "lost", None
        data: Dict[str, Any] = {
            "orderId": order.order_id,
            "status": order.status,
            "totalAmount": TO_PAY,
            "restaurantName": RESTAURANT["name"],
            "addressId": ADDRESS["id"],
            "cartId": CART_ID,
            "paymentMethod": method,
        }
        if method == "UPI":
            data.update(
                normalizedStatus="pending",
                paasId=order.paas_id,
                transactionId=order.transaction_id,
                upiIntentUrl=f"upi://pay?pa=carbon@upi&am={TO_PAY}&tr={order.transaction_id}",
                isQrFlow=bool(args.get("generateUPIQR")),
                pollingIntervalInMs=self.scenario.poll_interval_ms,
                maxTimeToPollForInMs=self.scenario.max_poll_ms,
                lat=ADDRESS["lat"],
                lng=ADDRESS["lng"],
            )
        else:
            data.update(normalizedStatus="success", estimatedDelivery="35 mins")
        return "ok", data

    def _order(self, order_id: Optional[str] = None, paas_id: Optional[str] = None) -> Order:
        for o in self.orders:
            if (order_id and o.order_id == order_id) or (paas_id and o.paas_id == paas_id):
                return o
        if paas_id:
            raise ToolError(f"Invalid paasId: {paas_id}")
        raise ToolError(f"Invalid orderId: {order_id}")

    def check_payment_status(self, args: Dict[str, Any]) -> Outcome:
        paas_id = args.get("paasId")
        if not paas_id:
            raise ToolError("Missing paasId (the paasId from place_food_order, not the payment method)")
        order = self._order(paas_id=paas_id)
        order.polls += 1
        self.polls.setdefault(order.order_id, []).append(self.now())
        if order.polls >= self.scenario.polls_until_paid and order.status != "FAILED":
            order.paid = True
        failed = order.status == "FAILED"
        return "ok", {
            "paasId": order.paas_id,
            "orderId": order.order_id,
            "status": "SUCCESS" if order.paid else "FAILED" if failed else "PENDING",
            "terminal": order.paid or failed,
            "isTerminalSuccess": order.paid,
            "isTerminalFailure": failed,
            "confirmed": order.confirmed,
            "orderStatus": order.status,
            "cartTotal": TO_PAY,
        }

    def confirm_order(self, args: Dict[str, Any]) -> Outcome:
        order_id = args.get("orderId")
        if not order_id:
            raise ToolError("Missing orderId")
        order = self._order(order_id=order_id)
        if self._confirm_failures_left > 0:
            self._confirm_failures_left -= 1
            return "http", (502, "Upstream error: payment confirmation service unavailable")
        if order.method == "UPI" and not order.confirmed:
            if order.paid:
                order.confirmed = True
                order.status = "PLACED"
            else:
                # The docs: the backend marks the order failed if payment is still non-terminal.
                order.status = "FAILED"
        result = "success" if order.confirmed else "failed"
        return "ok", {"orderId": order.order_id, "paasId": order.paas_id, "orderStatus": order.status,
                      "result": result}

    def get_food_orders(self, args: Dict[str, Any]) -> Outcome:
        return "ok", {"orders": [o.public() for o in reversed(self.orders)]}

    def track_food_order(self, args: Dict[str, Any]) -> Outcome:
        wanted = args.get("orderId")
        orders = [o for o in self.orders if not wanted or o.order_id == wanted]
        return "ok", {
            "orders": [
                {"orderId": o.order_id, "title": RESTAURANT["name"], "orderStatus": o.status,
                 "subtitle": "Payment pending" if o.status == "PENDING_PAYMENT" else o.status.title()}
                for o in orders if o.status in ("CONFIRMED", "PLACED", "PENDING_PAYMENT")
            ]
        }


TOOL_HANDLERS = {
    name: getattr(Shop, name)
    for name in (
        "get_addresses", "get_food_cart", "get_payment_options", "place_food_order",
        "check_payment_status", "confirm_order", "get_food_orders", "track_food_order",
    )
}

_STR = {"type": "string"}
TOOLS = [
    {"name": "get_addresses", "description": "List the user's saved delivery addresses.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_food_cart",
     "description": "Get the cart for an address: items, pricing and availablePaymentMethods.",
     "inputSchema": {"type": "object", "properties": {"addressId": _STR, "restaurantName": _STR},
                     "required": ["addressId"]}},
    {"name": "get_payment_options", "description": "Payment methods available for the cart.",
     "inputSchema": {"type": "object", "properties": {"addressId": _STR}}},
    {"name": "place_food_order",
     "description": ("Place the cart as an order. Cash orders are CONFIRMED. UPI orders start as "
                     "PENDING_PAYMENT and are NOT placed until payment succeeds and confirm_order "
                     "succeeds. Not idempotent: do not blind-retry on a network failure."),
     "inputSchema": {"type": "object", "properties": {
         "addressId": _STR, "paymentMethod": _STR, "intentApp": _STR,
         "generateUPIQR": {"type": "boolean"}, "noteToRestaurant": _STR},
         "required": ["addressId"]}},
    {"name": "check_payment_status",
     "description": "Read-only payment status for a UPI order. Poll at pollingIntervalInMs.",
     "inputSchema": {"type": "object", "properties": {
         "paasId": _STR, "orderId": _STR, "addressId": _STR, "cartId": _STR,
         "lat": {"type": "number"}, "lng": {"type": "number"}}, "required": ["paasId"]}},
    {"name": "confirm_order",
     "description": ("Finalize a UPI order after payment. Idempotent for the same order: retry on a "
                     "transient error."),
     "inputSchema": {"type": "object", "properties": {
         "orderId": _STR, "addressId": _STR, "cartId": _STR,
         "lat": {"type": "number"}, "lng": {"type": "number"}}, "required": ["orderId"]}},
    {"name": "get_food_orders", "description": "Recent orders, newest first.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "track_food_order", "description": "Active orders, or one order by orderId.",
     "inputSchema": {"type": "object", "properties": {"orderId": _STR}}},
]

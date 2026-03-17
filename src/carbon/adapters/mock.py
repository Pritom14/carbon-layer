"""Mock adapter: full protocol, no network. For MVP and tests."""

import uuid
from typing import Any

from carbon.adapters.base import PaymentAdapter


class MockAdapter:
    provider_name = "mock"

    def __init__(self) -> None:
        self._orders: dict[str, dict] = {}
        self._payments: dict[str, dict] = {}
        self._disputes: dict[str, dict] = {}
        self._refunds: dict[str, list[dict]] = {}

    async def validate_connection(self) -> bool:
        return True

    async def create_customer(self, params: dict) -> dict:
        cid = f"cust_mock_{uuid.uuid4().hex[:12]}"
        return {"id": cid, "entity": "customer", **params}

    async def create_order(self, params: dict) -> dict:
        oid = f"order_mock_{uuid.uuid4().hex[:12]}"
        amount = params.get("amount", params.get("amount_paise", 10000))
        rec = {
            "id": oid,
            "entity": "order",
            "amount": amount,
            "amount_paise": amount,
            "currency": "INR",
            "status": "created",
            "receipt": params.get("receipt", f"rcpt_{uuid.uuid4().hex[:8]}"),
        }
        self._orders[oid] = rec
        return rec

    async def create_payment(self, order_id: str, params: dict) -> dict:
        pid = f"pay_mock_{uuid.uuid4().hex[:12]}"
        amount = params.get("amount", self._orders.get(order_id, {}).get("amount", 10000))
        success = params.get("success", True) if "success" in params else True
        rec = {
            "id": pid,
            "entity": "payment",
            "order_id": order_id,
            "amount": amount,
            "currency": "INR",
            "status": "authorized" if success else "failed",
            "captured": False,
        }
        self._payments[pid] = rec
        return rec

    async def capture_payment(self, payment_id: str, amount: int) -> dict:
        p = self._payments.get(payment_id, {})
        p["status"] = "captured"
        p["captured"] = True
        p["amount"] = amount
        self._payments[payment_id] = p
        return p

    async def create_refund(self, payment_id: str, params: dict) -> dict:
        rid = f"rfnd_mock_{uuid.uuid4().hex[:12]}"
        amount = params.get("amount")
        if not amount and payment_id in self._payments:
            amount = self._payments[payment_id].get("amount", 0)
        rec = {"id": rid, "entity": "refund", "payment_id": payment_id, "amount": amount or 0, "status": "processed"}
        self._refunds.setdefault(payment_id, []).append(rec)
        return rec

    async def create_dispute(self, payment_id: str, params: dict) -> dict:
        did = f"disp_mock_{uuid.uuid4().hex[:12]}"
        rec = {
            "id": did,
            "entity": "dispute",
            "payment_id": payment_id,
            "status": "open",
            **params,
        }
        self._disputes[did] = rec
        return rec

    async def fetch_order(self, order_id: str) -> dict:
        return self._orders.get(order_id, {"id": order_id, "status": "unknown"})

    async def fetch_payment(self, payment_id: str) -> dict:
        return self._payments.get(payment_id, {"id": payment_id, "status": "unknown"})

    async def list_disputes(self, filters: dict) -> list[dict]:
        return list(self._disputes.values())

    async def list_refunds(self, payment_id: str) -> list[dict]:
        return self._refunds.get(payment_id, [])


def get_mock_adapter() -> PaymentAdapter:
    return MockAdapter()

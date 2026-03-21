"""Cashfree Payments API client (async httpx)."""

from typing import Any, Optional

import httpx

SANDBOX_URL = "https://sandbox.cashfree.com/pg"
PRODUCTION_URL = "https://api.cashfree.com/pg"
API_VERSION = "2025-01-01"


class CashfreeClient:
    def __init__(self, client_id: str, client_secret: str, sandbox: bool = True) -> None:
        base_url = SANDBOX_URL if sandbox else PRODUCTION_URL
        self._client_secret = client_secret
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "x-client-id": client_id,
                "x-client-secret": client_secret,
                "x-api-version": API_VERSION,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def post(self, path: str, json: Optional[dict] = None) -> dict:
        r = await self._client.post(path, json=json or {})
        r.raise_for_status()
        return r.json() if r.content else {}

    async def get(self, path: str, params: Optional[dict] = None) -> Any:
        r = await self._client.get(path, params=params or {})
        r.raise_for_status()
        return r.json() if r.content else {}

    # Orders
    async def create_order(
        self,
        amount: float,
        currency: str = "INR",
        customer_id: str = "carbon_test_customer",
        customer_phone: str = "9999999999",
        order_id: Optional[str] = None,
        order_note: Optional[str] = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "order_amount": amount,
            "order_currency": currency,
            "customer_details": {
                "customer_id": customer_id,
                "customer_phone": customer_phone,
            },
        }
        if order_id:
            payload["order_id"] = order_id
        if order_note:
            payload["order_note"] = order_note
        return await self.post("/orders", json=payload)

    async def fetch_order(self, order_id: str) -> dict:
        return await self.get(f"/orders/{order_id}")

    # Payments
    async def fetch_payments_for_order(self, order_id: str) -> list[dict]:
        result = await self.get(f"/orders/{order_id}/payments")
        if isinstance(result, list):
            return result
        return result.get("data", [result]) if isinstance(result, dict) else []

    async def fetch_payment(self, order_id: str, cf_payment_id: str) -> dict:
        return await self.get(f"/orders/{order_id}/payments/{cf_payment_id}")

    async def capture_payment(self, order_id: str, amount: float) -> dict:
        return await self.post(
            f"/orders/{order_id}/authorization",
            json={"action": "CAPTURE", "amount": amount},
        )

    # Refunds
    async def create_refund(
        self,
        order_id: str,
        refund_amount: float,
        refund_id: str,
        refund_note: Optional[str] = None,
        refund_speed: str = "STANDARD",
    ) -> dict:
        payload: dict[str, Any] = {
            "refund_amount": refund_amount,
            "refund_id": refund_id,
            "refund_speed": refund_speed,
        }
        if refund_note:
            payload["refund_note"] = refund_note
        return await self.post(f"/orders/{order_id}/refunds", json=payload)

    async def fetch_refund(self, order_id: str, refund_id: str) -> dict:
        return await self.get(f"/orders/{order_id}/refunds/{refund_id}")

    # Disputes (read-only)
    async def fetch_disputes_for_order(self, order_id: str) -> list[dict]:
        result = await self.get(f"/orders/{order_id}/disputes")
        if isinstance(result, list):
            return result
        return result.get("data", []) if isinstance(result, dict) else []

    async def fetch_disputes_for_payment(self, cf_payment_id: str) -> list[dict]:
        result = await self.get(f"/payments/{cf_payment_id}/disputes")
        if isinstance(result, list):
            return result
        return result.get("data", []) if isinstance(result, dict) else []

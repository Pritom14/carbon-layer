"""Razorpay API client (async httpx)."""

import base64
from typing import Any, Optional

import httpx

BASE_URL = "https://api.razorpay.com/v1"


class RazorpayClient:
    def __init__(self, api_key: str, api_secret: str) -> None:
        self._auth = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Basic {self._auth}",
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
    async def create_order(self, amount: int, currency: str = "INR", receipt: Optional[str] = None, notes: Optional[dict] = None) -> dict:
        payload = {"amount": amount, "currency": currency}
        if receipt:
            payload["receipt"] = receipt
        if notes:
            payload["notes"] = notes
        return await self.post("/orders", json=payload)

    async def fetch_order(self, order_id: str) -> dict:
        return await self.get(f"/orders/{order_id}")

    # Payments
    async def capture_payment(self, payment_id: str, amount: int, currency: str = "INR") -> dict:
        return await self.post(f"/payments/{payment_id}/capture", json={"amount": amount, "currency": currency})

    async def fetch_payment(self, payment_id: str) -> dict:
        return await self.get(f"/payments/{payment_id}")

    # Refunds
    async def create_refund(self, payment_id: str, amount: Optional[int] = None, notes: Optional[dict] = None) -> dict:
        payload = {}
        if amount is not None:
            payload["amount"] = amount
        if notes:
            payload["notes"] = notes
        return await self.post(f"/payments/{payment_id}/refund", json=payload or {})

    # Disputes (read-only)
    async def list_disputes(self, params: Optional[dict] = None) -> dict:
        out = await self.get("/disputes", params=params or {})
        if isinstance(out, dict) and "items" in out:
            return out
        return {"items": out if isinstance(out, list) else []}

    async def list_refunds(self, payment_id: str) -> list[dict]:
        out = await self.get(f"/payments/{payment_id}/refunds")
        if isinstance(out, dict) and "items" in out:
            return out["items"]
        return out if isinstance(out, list) else []

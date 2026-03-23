"""Juspay API client (async httpx). Uses HTTP Basic Auth with API key and empty password."""

import base64
from typing import Any, Optional

import httpx

SANDBOX_URL = "https://sandbox.juspay.in"
PRODUCTION_URL = "https://api.juspay.in"


class JuspayClient:
    def __init__(self, api_key: str, merchant_id: str, sandbox: bool = True) -> None:
        base_url = SANDBOX_URL if sandbox else PRODUCTION_URL
        # Juspay uses Basic Auth: Base64(api_key:) — empty password
        auth_token = base64.b64encode(f"{api_key}:".encode()).decode()
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Basic {auth_token}",
                "x-merchantid": merchant_id,
            },
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def post_form(self, path: str, data: Optional[dict] = None) -> dict:
        """POST with application/x-www-form-urlencoded (Orders, Refunds, Txns)."""
        r = await self._client.post(
            path,
            data=data or {},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        return r.json() if r.content else {}

    async def post_json(self, path: str, json: Optional[dict] = None) -> dict:
        """POST with application/json (Session API)."""
        r = await self._client.post(
            path,
            json=json or {},
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        return r.json() if r.content else {}

    async def get(self, path: str, params: Optional[dict] = None) -> Any:
        r = await self._client.get(path, params=params or {})
        r.raise_for_status()
        return r.json() if r.content else {}

    # Orders
    async def create_order(
        self,
        order_id: str,
        amount: float,
        currency: str = "INR",
        customer_id: str = "carbon_test_customer",
        customer_email: str = "test@carbon.dev",
        customer_phone: str = "9999999999",
    ) -> dict:
        return await self.post_form("/orders", data={
            "order_id": order_id,
            "amount": str(amount),
            "currency": currency,
            "customer_id": customer_id,
            "customer_email": customer_email,
            "customer_phone": customer_phone,
        })

    async def fetch_order(self, order_id: str) -> dict:
        return await self.get(f"/orders/{order_id}")

    # Refunds
    async def create_refund(
        self,
        order_id: str,
        unique_request_id: str,
        amount: Optional[float] = None,
    ) -> dict:
        data: dict[str, str] = {"unique_request_id": unique_request_id}
        if amount is not None:
            data["amount"] = str(amount)
        return await self.post_form(f"/orders/{order_id}/refunds", data=data)

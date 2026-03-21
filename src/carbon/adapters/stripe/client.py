"""Stripe API client (async httpx)."""

from typing import Any, Optional

import httpx

BASE_URL = "https://api.stripe.com/v1"


class StripeClient:
    def __init__(self, api_key: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
            },
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def post(self, path: str, data: Optional[dict] = None) -> dict:
        r = await self._client.post(path, data=data or {})
        r.raise_for_status()
        return r.json() if r.content else {}

    async def get(self, path: str, params: Optional[dict] = None) -> Any:
        r = await self._client.get(path, params=params or {})
        r.raise_for_status()
        return r.json() if r.content else {}

    # Payment Intents (Stripe's equivalent of orders + payments)
    async def create_payment_intent(
        self, amount: int, currency: str = "usd", metadata: Optional[dict] = None
    ) -> dict:
        payload: dict[str, Any] = {"amount": amount, "currency": currency}
        if metadata:
            for k, v in metadata.items():
                payload[f"metadata[{k}]"] = v
        return await self.post("/payment_intents", data=payload)

    async def confirm_payment_intent(self, pi_id: str, payment_method: str = "pm_card_visa") -> dict:
        return await self.post(f"/payment_intents/{pi_id}/confirm", data={"payment_method": payment_method})

    async def capture_payment_intent(self, pi_id: str) -> dict:
        return await self.post(f"/payment_intents/{pi_id}/capture")

    async def fetch_payment_intent(self, pi_id: str) -> dict:
        return await self.get(f"/payment_intents/{pi_id}")

    # Refunds
    async def create_refund(self, payment_intent_id: str, amount: Optional[int] = None) -> dict:
        payload: dict[str, Any] = {"payment_intent": payment_intent_id}
        if amount is not None:
            payload["amount"] = amount
        return await self.post("/refunds", data=payload)

    async def fetch_refund(self, refund_id: str) -> dict:
        return await self.get(f"/refunds/{refund_id}")

    async def list_refunds(self, payment_intent_id: str) -> list[dict]:
        out = await self.get("/refunds", params={"payment_intent": payment_intent_id})
        return out.get("data", [])

    # Disputes (read-only)
    async def list_disputes(self, params: Optional[dict] = None) -> list[dict]:
        out = await self.get("/disputes", params=params or {})
        return out.get("data", [])

    # Charges (needed for some lookups)
    async def list_charges(self, payment_intent_id: str) -> list[dict]:
        out = await self.get("/charges", params={"payment_intent": payment_intent_id})
        return out.get("data", [])

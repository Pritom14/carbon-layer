"""Razorpay adapter: implements only what the API supports (no create_payment, no create_dispute)."""

from carbon.adapters.base import PaymentAdapter
from carbon.adapters.razorpay.client import RazorpayClient


class RazorpayAdapterError(Exception):
    """Raised when an operation is not supported by Razorpay API."""

    pass


class RazorpayAdapter:
    provider_name = "razorpay"

    def __init__(self, api_key: str, api_secret: str) -> None:
        self._client = RazorpayClient(api_key, api_secret)

    async def validate_connection(self) -> bool:
        try:
            await self._client.get("/orders", params={"count": 1})
            return True
        except Exception:
            return False

    async def create_customer(self, params: dict) -> dict:
        raise RazorpayAdapterError("create_customer not implemented in MVP")

    async def create_order(self, params: dict) -> dict:
        amount = params.get("amount") or params.get("amount_paise", 10000)
        receipt = params.get("receipt", "")
        notes = params.get("notes") or {}
        return await self._client.create_order(amount=amount, receipt=receipt, notes=notes)

    async def create_payment(self, order_id: str, params: dict) -> dict:
        raise RazorpayAdapterError(
            "Razorpay's server-side payment API (S2S) requires special permissions. "
            "Standard flow uses Checkout/Payment Link on the client side. Use --provider mock for full scenario runs."
        )

    async def capture_payment(self, payment_id: str, amount: int) -> dict:
        return await self._client.capture_payment(payment_id, amount=amount)

    async def create_refund(self, payment_id: str, params: dict) -> dict:
        amount = params.get("amount")
        notes = params.get("notes")
        return await self._client.create_refund(payment_id, amount=amount, notes=notes)

    async def create_dispute(self, payment_id: str, params: dict) -> dict:
        raise RazorpayAdapterError(
            "Razorpay does not allow creating disputes via API (disputes come from bank/chargebacks). "
            "Use --provider mock for dispute-spike scenarios."
        )

    async def fetch_order(self, order_id: str) -> dict:
        return await self._client.fetch_order(order_id)

    async def fetch_payment(self, payment_id: str) -> dict:
        return await self._client.fetch_payment(payment_id)

    async def list_disputes(self, filters: dict) -> list[dict]:
        data = await self._client.list_disputes(params=filters)
        return data.get("items", [])

    async def list_refunds(self, payment_id: str) -> list[dict]:
        return await self._client.list_refunds(payment_id)

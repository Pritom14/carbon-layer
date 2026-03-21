"""Stripe adapter: implements what the API supports."""

from carbon.adapters.base import PaymentAdapter
from carbon.adapters.stripe.client import StripeClient


class StripeAdapterError(Exception):
    """Raised when an operation is not supported by Stripe API."""

    pass


class StripeAdapter:
    provider_name = "stripe"

    def __init__(self, api_key: str) -> None:
        self._client = StripeClient(api_key)

    async def validate_connection(self) -> bool:
        try:
            await self._client.get("/payment_intents", params={"limit": 1})
            return True
        except Exception:
            return False

    async def create_customer(self, params: dict) -> dict:
        raise StripeAdapterError("create_customer not implemented in MVP")

    async def create_order(self, params: dict) -> dict:
        amount = params.get("amount") or params.get("amount_cents", 1000)
        currency = params.get("currency", "usd")
        metadata = params.get("metadata") or {}
        result = await self._client.create_payment_intent(
            amount=amount, currency=currency, metadata=metadata
        )
        # Normalize to common order-like shape
        return {
            "id": result["id"],
            "amount": result["amount"],
            "currency": result["currency"],
            "status": result["status"],
            "receipt": result.get("metadata", {}).get("receipt", ""),
        }

    async def create_payment(self, order_id: str, params: dict) -> dict:
        # In Stripe, confirming a PaymentIntent is how you create a payment
        payment_method = params.get("payment_method", "pm_card_visa")
        success = params.get("success", True)
        if not success:
            payment_method = "pm_card_chargeDeclined"
        result = await self._client.confirm_payment_intent(order_id, payment_method=payment_method)
        return {
            "id": result["id"],
            "order_id": order_id,
            "amount": result["amount"],
            "status": result["status"],
        }

    async def capture_payment(self, payment_id: str, amount: int) -> dict:
        result = await self._client.capture_payment_intent(payment_id)
        return {
            "id": result["id"],
            "amount": result["amount"],
            "status": result["status"],
        }

    async def create_refund(self, payment_id: str, params: dict) -> dict:
        amount = params.get("amount")
        result = await self._client.create_refund(payment_id, amount=amount)
        return {
            "id": result["id"],
            "payment_id": payment_id,
            "amount": result["amount"],
            "status": result["status"],
        }

    async def create_dispute(self, payment_id: str, params: dict) -> dict:
        raise StripeAdapterError(
            "Stripe does not allow creating disputes via API (disputes come from cardholders). "
            "Use --provider mock for dispute-spike scenarios."
        )

    async def fetch_order(self, order_id: str) -> dict:
        return await self._client.fetch_payment_intent(order_id)

    async def fetch_payment(self, payment_id: str) -> dict:
        return await self._client.fetch_payment_intent(payment_id)

    async def list_disputes(self, filters: dict) -> list[dict]:
        return await self._client.list_disputes(params=filters)

    async def list_refunds(self, payment_id: str) -> list[dict]:
        return await self._client.list_refunds(payment_id)

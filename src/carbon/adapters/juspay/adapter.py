"""Juspay adapter: implements what the API supports."""

import uuid

from carbon.adapters.base import PaymentAdapter
from carbon.adapters.juspay.client import JuspayClient


class JuspayAdapterError(Exception):
    """Raised when an operation is not supported by Juspay API."""

    pass


class JuspayAdapter:
    provider_name = "juspay"

    def __init__(self, api_key: str, merchant_id: str, sandbox: bool = True) -> None:
        self._client = JuspayClient(api_key, merchant_id, sandbox=sandbox)

    async def validate_connection(self) -> bool:
        try:
            test_id = f"carbon_test_{uuid.uuid4().hex[:8]}"
            result = await self._client.create_order(
                order_id=test_id, amount=1.00,
            )
            return result.get("status") == "NEW"
        except Exception:
            return False

    async def create_customer(self, params: dict) -> dict:
        raise JuspayAdapterError("create_customer not implemented in MVP")

    async def create_order(self, params: dict) -> dict:
        amount = params.get("amount") or params.get("amount_paise", 10000)
        if isinstance(amount, int) and amount > 100:
            amount = amount / 100
        currency = params.get("currency", "INR")
        order_id = params.get("order_id") or f"carbon_{uuid.uuid4().hex[:12]}"

        result = await self._client.create_order(
            order_id=order_id,
            amount=float(amount),
            currency=currency,
            customer_id=params.get("customer_id", "carbon_test_customer"),
            customer_email=params.get("customer_email", "test@carbon.dev"),
            customer_phone=params.get("customer_phone", "9999999999"),
        )
        return {
            "id": result.get("order_id", order_id),
            "amount": result.get("amount", amount),
            "currency": result.get("currency", currency),
            "status": result.get("status", "NEW"),
        }

    async def create_payment(self, order_id: str, params: dict) -> dict:
        raise JuspayAdapterError(
            "Juspay payment initiation (POST /txns) requires card/UPI details and customer redirect. "
            "Use --provider mock for full scenario runs."
        )

    async def capture_payment(self, payment_id: str, amount: int) -> dict:
        raise JuspayAdapterError(
            "Juspay auto-captures payments on success (status=CHARGED). "
            "Manual capture is not supported. Use --provider mock for full scenario runs."
        )

    async def create_refund(self, payment_id: str, params: dict) -> dict:
        order_id = params.get("order_id", payment_id)
        amount = params.get("amount")
        if isinstance(amount, int) and amount > 100:
            amount = amount / 100
        unique_request_id = params.get("unique_request_id") or f"rfnd_{uuid.uuid4().hex[:12]}"

        result = await self._client.create_refund(
            order_id=order_id,
            unique_request_id=unique_request_id,
            amount=float(amount) if amount else None,
        )
        return {
            "id": unique_request_id,
            "order_id": order_id,
            "amount": result.get("amount_refunded", amount),
            "status": result.get("status", "PENDING"),
        }

    async def create_dispute(self, payment_id: str, params: dict) -> dict:
        raise JuspayAdapterError(
            "Juspay does not expose a dispute creation API (disputes are managed via dashboard). "
            "Use --provider mock for dispute-spike scenarios."
        )

    async def fetch_order(self, order_id: str) -> dict:
        return await self._client.fetch_order(order_id)

    async def fetch_payment(self, payment_id: str) -> dict:
        # Juspay ties payments to orders — fetch order to get payment status
        return await self._client.fetch_order(payment_id)

    async def list_disputes(self, filters: dict) -> list[dict]:
        return []  # Juspay disputes are dashboard-only

    async def list_refunds(self, payment_id: str) -> list[dict]:
        # Refund info is embedded in the order response
        order = await self._client.fetch_order(payment_id)
        refunds = order.get("refunds", [])
        return refunds if isinstance(refunds, list) else []

"""Cashfree adapter: implements what the API supports."""

import uuid

from carbon.adapters.base import PaymentAdapter
from carbon.adapters.cashfree.client import CashfreeClient


class CashfreeAdapterError(Exception):
    """Raised when an operation is not supported by Cashfree API."""

    pass


class CashfreeAdapter:
    provider_name = "cashfree"

    def __init__(self, client_id: str, client_secret: str, sandbox: bool = True) -> None:
        self._client = CashfreeClient(client_id, client_secret, sandbox=sandbox)
        self._client_secret = client_secret

    async def validate_connection(self) -> bool:
        try:
            # Create a minimal order to test credentials
            result = await self._client.create_order(
                amount=1.00,
                order_id=f"carbon_test_{uuid.uuid4().hex[:8]}",
                order_note="connection test",
            )
            return bool(result.get("cf_order_id") or result.get("order_id"))
        except Exception:
            return False

    async def create_customer(self, params: dict) -> dict:
        raise CashfreeAdapterError("create_customer not implemented in MVP")

    async def create_order(self, params: dict) -> dict:
        # Cashfree uses rupees (float), not paise (int)
        amount = params.get("amount") or params.get("amount_paise", 10000)
        if isinstance(amount, int) and amount > 100:
            # Likely paise from scenario — convert to rupees
            amount = amount / 100
        currency = params.get("currency", "INR")
        customer_id = params.get("customer_id", "carbon_test_customer")
        customer_phone = params.get("customer_phone", "9999999999")
        order_id = params.get("order_id") or f"carbon_{uuid.uuid4().hex[:12]}"

        result = await self._client.create_order(
            amount=float(amount),
            currency=currency,
            customer_id=customer_id,
            customer_phone=customer_phone,
            order_id=order_id,
        )
        return {
            "id": result.get("order_id") or result.get("cf_order_id", ""),
            "cf_order_id": result.get("cf_order_id", ""),
            "amount": result.get("order_amount", amount),
            "currency": result.get("order_currency", currency),
            "status": result.get("order_status", "ACTIVE"),
        }

    async def create_payment(self, order_id: str, params: dict) -> dict:
        raise CashfreeAdapterError(
            "Cashfree's server-side payment API (Order Pay) requires S2S flag enabled "
            "and PCI DSS certification for card data. Use --provider mock for full scenario runs."
        )

    async def capture_payment(self, payment_id: str, amount: int) -> dict:
        # payment_id is order_id in Cashfree's preauth flow
        capture_amount = float(amount) / 100 if amount > 100 else float(amount)
        result = await self._client.capture_payment(payment_id, amount=capture_amount)
        return {
            "id": payment_id,
            "amount": capture_amount,
            "status": result.get("payment_status", "SUCCESS"),
        }

    async def create_refund(self, payment_id: str, params: dict) -> dict:
        order_id = params.get("order_id", payment_id)
        amount = params.get("amount")
        if isinstance(amount, int) and amount > 100:
            amount = amount / 100
        refund_id = params.get("refund_id") or f"rfnd_{uuid.uuid4().hex[:12]}"

        result = await self._client.create_refund(
            order_id=order_id,
            refund_amount=float(amount) if amount else 0,
            refund_id=refund_id,
            refund_note=params.get("refund_note"),
        )
        return {
            "id": result.get("refund_id") or result.get("cf_refund_id", ""),
            "cf_refund_id": result.get("cf_refund_id", ""),
            "payment_id": payment_id,
            "amount": result.get("refund_amount", amount),
            "status": result.get("refund_status", "PENDING"),
        }

    async def create_dispute(self, payment_id: str, params: dict) -> dict:
        raise CashfreeAdapterError(
            "Cashfree does not allow creating disputes via API (disputes come from cardholders/banks). "
            "Use --provider mock for dispute-spike scenarios."
        )

    async def fetch_order(self, order_id: str) -> dict:
        return await self._client.fetch_order(order_id)

    async def fetch_payment(self, payment_id: str) -> dict:
        # Cashfree needs order_id to fetch payments — use payment_id as order_id fallback
        payments = await self._client.fetch_payments_for_order(payment_id)
        return payments[0] if payments else {"id": payment_id, "status": "unknown"}

    async def list_disputes(self, filters: dict) -> list[dict]:
        order_id = filters.get("order_id")
        cf_payment_id = filters.get("cf_payment_id")
        if cf_payment_id:
            return await self._client.fetch_disputes_for_payment(cf_payment_id)
        if order_id:
            return await self._client.fetch_disputes_for_order(order_id)
        return []

    async def list_refunds(self, payment_id: str) -> list[dict]:
        # Cashfree uses order_id for refund listing
        result = await self._client.get(f"/orders/{payment_id}/refunds")
        if isinstance(result, list):
            return result
        return result.get("data", []) if isinstance(result, dict) else []

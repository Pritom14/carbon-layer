"""Adapter registry: get adapter by provider name."""

from __future__ import annotations

from carbon.adapters.base import PaymentAdapter
from carbon.adapters.mock import get_mock_adapter
from carbon.adapters.razorpay import RazorpayAdapter
from carbon.adapters.stripe import StripeAdapter
from carbon.adapters.cashfree import CashfreeAdapter


def get_adapter(
    provider: str,
    api_key: str | None = None,
    api_secret: str | None = None,
) -> PaymentAdapter:
    if provider == "mock":
        return get_mock_adapter()
    if provider == "razorpay":
        if not api_key or not api_secret:
            return get_mock_adapter()
        return RazorpayAdapter(api_key=api_key, api_secret=api_secret)
    if provider == "stripe":
        if not api_key:
            return get_mock_adapter()
        return StripeAdapter(api_key=api_key)
    if provider == "cashfree":
        if not api_key or not api_secret:
            return get_mock_adapter()
        return CashfreeAdapter(client_id=api_key, client_secret=api_secret)
    raise ValueError(f"Unknown provider: {provider}")

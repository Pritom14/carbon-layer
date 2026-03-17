"""Adapter registry: get adapter by provider name."""

from __future__ import annotations

from carbon.adapters.base import PaymentAdapter
from carbon.adapters.mock import get_mock_adapter
from carbon.adapters.razorpay import RazorpayAdapter


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
    raise ValueError(f"Unknown provider: {provider}")

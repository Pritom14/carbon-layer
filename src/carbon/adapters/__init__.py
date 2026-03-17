"""Payment provider adapters."""

from carbon.adapters.base import PaymentAdapter
from carbon.adapters.registry import get_adapter

__all__ = ["PaymentAdapter", "get_adapter"]

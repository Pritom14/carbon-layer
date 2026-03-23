"""Configuration via environment and .env."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    razorpay_api_key: Optional[str] = None
    razorpay_api_secret: Optional[str] = None
    stripe_api_key: Optional[str] = None
    cashfree_client_id: Optional[str] = None
    cashfree_client_secret: Optional[str] = None
    juspay_api_key: Optional[str] = None
    juspay_merchant_id: Optional[str] = None
    carbon_scenarios_dir: Path = Path("scenarios")

    # SQLite (default, zero config) or PostgreSQL.
    # SQLite:     sqlite:///~/.carbon/carbon.db  (default)
    # PostgreSQL: postgresql://localhost:5432/carbon
    database_url: str = "sqlite:///~/.carbon/carbon.db"

    @property
    def has_razorpay_credentials(self) -> bool:
        return bool(self.razorpay_api_key and self.razorpay_api_secret)

    @property
    def has_stripe_credentials(self) -> bool:
        return bool(self.stripe_api_key)

    @property
    def has_cashfree_credentials(self) -> bool:
        return bool(self.cashfree_client_id and self.cashfree_client_secret)

    @property
    def has_juspay_credentials(self) -> bool:
        return bool(self.juspay_api_key and self.juspay_merchant_id)


def get_settings() -> Settings:
    return Settings()

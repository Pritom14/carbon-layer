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
    carbon_scenarios_dir: Path = Path("scenarios")

    # PostgreSQL (required). Default: local DB "carbon" on port 5432.
    database_url: str = "postgresql://localhost:5432/carbon"

    @property
    def has_razorpay_credentials(self) -> bool:
        return bool(self.razorpay_api_key and self.razorpay_api_secret)


def get_settings() -> Settings:
    return Settings()

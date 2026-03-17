"""Pytest configuration and fixtures for Carbon Layer tests."""

from __future__ import annotations

import os

import pytest


def pytest_configure(config):
    """Register markers."""
    config.addinivalue_line(
        "markers",
        "integration: marks tests that require PostgreSQL (set DATABASE_URL or CARBON_TEST_DATABASE_URL)",
    )


@pytest.fixture(scope="session")
def database_url():
    """Database URL for integration tests. Skip if not set."""
    url = os.environ.get("CARBON_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url or not url.strip():
        pytest.skip(
            "PostgreSQL required: set CARBON_TEST_DATABASE_URL or DATABASE_URL for integration tests"
        )
    if not url.startswith("postgresql"):
        pytest.skip("Integration tests require PostgreSQL (postgresql://...)")
    return url


@pytest.fixture
def set_database_url(database_url):
    """Set DATABASE_URL for the duration of the test (for integration tests)."""
    orig = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    yield
    if orig is not None:
        os.environ["DATABASE_URL"] = orig
    elif "DATABASE_URL" in os.environ:
        del os.environ["DATABASE_URL"]


@pytest.fixture
async def ensure_db(set_database_url):
    """Ensure DB is initialized (for integration tests)."""
    from carbon.storage.db import init_db
    await init_db()
    yield

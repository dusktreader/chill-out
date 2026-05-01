"""Shared fixtures for ecosystem-level unit tests."""

import httpx
import pytest


@pytest.fixture
async def http_client():
    """An async HTTP client that lives for the duration of a single test."""
    async with httpx.AsyncClient() as client:
        yield client

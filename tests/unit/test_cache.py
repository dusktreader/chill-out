"""Unit tests for the in-memory CachingRegistryClient."""

from __future__ import annotations

import asyncio

import httpx
import pendulum
import pytest
from chill_out.cache import CachingRegistryClient
from chill_out.ecosystems.base import RegistryClient
from chill_out.models import PackageInfo, PackageRelease, VersionManifest


class _CountingClient(RegistryClient):
    def __init__(self, http: httpx.AsyncClient) -> None:
        super().__init__(http)
        self.package_calls: list[str] = []
        self.manifest_calls: list[tuple[str, str]] = []

    async def fetch_package(self, name: str) -> PackageInfo | None:
        self.package_calls.append(name)
        # Simulate a slow network so concurrent callers actually overlap.
        await asyncio.sleep(0.01)
        if name == "missing":
            return None
        return PackageInfo(
            name=name,
            releases={"1.0.0": PackageRelease("1.0.0", pendulum.datetime(2025, 1, 1, tz="UTC"))},
        )

    async def fetch_version_manifest(self, name: str, version: str) -> VersionManifest | None:
        self.manifest_calls.append((name, version))
        await asyncio.sleep(0.01)
        if name == "missing":
            return None
        return VersionManifest(name=name, version=version, deps={"dep": ">=1.0"})


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as client:
        yield client


class TestCachingRegistryClient:
    @pytest.mark.asyncio
    async def test_repeat_fetch_package_hits_inner_once(self, http_client: httpx.AsyncClient) -> None:
        inner = _CountingClient(http_client)
        cache = CachingRegistryClient(inner)
        a = await cache.fetch_package("requests")
        b = await cache.fetch_package("requests")
        assert a is b
        assert inner.package_calls == ["requests"]

    @pytest.mark.asyncio
    async def test_concurrent_fetch_package_dedupes(self, http_client: httpx.AsyncClient) -> None:
        inner = _CountingClient(http_client)
        cache = CachingRegistryClient(inner)
        results = await asyncio.gather(*(cache.fetch_package("requests") for _ in range(5)))
        assert all(r is results[0] for r in results)
        assert inner.package_calls == ["requests"]

    @pytest.mark.asyncio
    async def test_none_results_cached(self, http_client: httpx.AsyncClient) -> None:
        inner = _CountingClient(http_client)
        cache = CachingRegistryClient(inner)
        first = await cache.fetch_package("missing")
        second = await cache.fetch_package("missing")
        assert first is None
        assert second is None
        assert inner.package_calls == ["missing"]

    @pytest.mark.asyncio
    async def test_repeat_fetch_manifest_hits_inner_once(self, http_client: httpx.AsyncClient) -> None:
        inner = _CountingClient(http_client)
        cache = CachingRegistryClient(inner)
        a = await cache.fetch_version_manifest("requests", "2.31.0")
        b = await cache.fetch_version_manifest("requests", "2.31.0")
        assert a is b
        assert inner.manifest_calls == [("requests", "2.31.0")]

    @pytest.mark.asyncio
    async def test_concurrent_fetch_manifest_dedupes(self, http_client: httpx.AsyncClient) -> None:
        inner = _CountingClient(http_client)
        cache = CachingRegistryClient(inner)
        results = await asyncio.gather(*(cache.fetch_version_manifest("requests", "2.31.0") for _ in range(5)))
        assert all(r is results[0] for r in results)
        assert inner.manifest_calls == [("requests", "2.31.0")]

    @pytest.mark.asyncio
    async def test_different_versions_fetched_independently(self, http_client: httpx.AsyncClient) -> None:
        inner = _CountingClient(http_client)
        cache = CachingRegistryClient(inner)
        await cache.fetch_version_manifest("requests", "2.31.0")
        await cache.fetch_version_manifest("requests", "2.30.0")
        assert inner.manifest_calls == [("requests", "2.31.0"), ("requests", "2.30.0")]

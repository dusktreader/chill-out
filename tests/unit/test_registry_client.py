"""Unit tests for the in-memory `RegistryClient` cache + dedupe layer."""

import asyncio
from typing import cast

import httpx
import pendulum
import pytest
from chill_out.ecosystems.backend import Ecosystem
from chill_out.models import PackageInfo, PackageRelease, VersionManifest
from chill_out.registry_client import RegistryClient


class _CountingFetcher:
    """Stand-in fetcher that records its calls and ignores the http client.

    Implements only the two `Ecosystem` methods `RegistryClient` actually uses;
    the call sites cast it through `Ecosystem` to keep the production type
    signature honest while letting these tests stay tightly focused on the
    cache + dedupe behavior.
    """

    def __init__(self) -> None:
        self.package_calls: list[str] = []
        self.manifest_calls: list[tuple[str, str]] = []

    async def fetch_package(self, name: str, http: httpx.AsyncClient) -> PackageInfo | None:
        self.package_calls.append(name)
        # Simulate a slow network so concurrent callers actually overlap.
        await asyncio.sleep(0.01)
        if name == "missing":
            return None
        return PackageInfo(
            name=name,
            releases={"1.0.0": PackageRelease("1.0.0", pendulum.datetime(2025, 1, 1, tz="UTC"))},
        )

    async def fetch_version_manifest(self, name: str, version: str, http: httpx.AsyncClient) -> VersionManifest | None:
        self.manifest_calls.append((name, version))
        await asyncio.sleep(0.01)
        if name == "missing":
            return None
        return VersionManifest(name=name, version=version, deps={"dep": ">=1.0"})


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as client:
        yield client


class TestRegistryClient:
    @pytest.mark.asyncio
    async def test_repeat_fetch_package_hits_fetcher_once(self, http_client: httpx.AsyncClient) -> None:
        fetcher = _CountingFetcher()
        client = RegistryClient(cast(Ecosystem, fetcher), http_client)
        a = await client.fetch_package("requests")
        b = await client.fetch_package("requests")
        assert a is b
        assert fetcher.package_calls == ["requests"]

    @pytest.mark.asyncio
    async def test_concurrent_fetch_package_dedupes(self, http_client: httpx.AsyncClient) -> None:
        fetcher = _CountingFetcher()
        client = RegistryClient(cast(Ecosystem, fetcher), http_client)
        results = await asyncio.gather(*(client.fetch_package("requests") for _ in range(5)))
        assert all(r is results[0] for r in results)
        assert fetcher.package_calls == ["requests"]

    @pytest.mark.asyncio
    async def test_none_results_cached(self, http_client: httpx.AsyncClient) -> None:
        fetcher = _CountingFetcher()
        client = RegistryClient(cast(Ecosystem, fetcher), http_client)
        first = await client.fetch_package("missing")
        second = await client.fetch_package("missing")
        assert first is None
        assert second is None
        assert fetcher.package_calls == ["missing"]

    @pytest.mark.asyncio
    async def test_repeat_fetch_manifest_hits_fetcher_once(self, http_client: httpx.AsyncClient) -> None:
        fetcher = _CountingFetcher()
        client = RegistryClient(cast(Ecosystem, fetcher), http_client)
        a = await client.fetch_version_manifest("requests", "2.31.0")
        b = await client.fetch_version_manifest("requests", "2.31.0")
        assert a is b
        assert fetcher.manifest_calls == [("requests", "2.31.0")]

    @pytest.mark.asyncio
    async def test_concurrent_fetch_manifest_dedupes(self, http_client: httpx.AsyncClient) -> None:
        fetcher = _CountingFetcher()
        client = RegistryClient(cast(Ecosystem, fetcher), http_client)
        results = await asyncio.gather(*(client.fetch_version_manifest("requests", "2.31.0") for _ in range(5)))
        assert all(r is results[0] for r in results)
        assert fetcher.manifest_calls == [("requests", "2.31.0")]

    @pytest.mark.asyncio
    async def test_different_versions_fetched_independently(self, http_client: httpx.AsyncClient) -> None:
        fetcher = _CountingFetcher()
        client = RegistryClient(cast(Ecosystem, fetcher), http_client)
        await client.fetch_version_manifest("requests", "2.31.0")
        await client.fetch_version_manifest("requests", "2.30.0")
        assert fetcher.manifest_calls == [("requests", "2.31.0"), ("requests", "2.30.0")]

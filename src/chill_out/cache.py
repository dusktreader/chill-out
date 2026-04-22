"""
Caching wrapper around :class:`RegistryClient`.

Per-process in-memory cache that deduplicates :meth:`fetch_package` and
:meth:`fetch_version_manifest` calls. The cache stores the *result* (including
``None``) keyed by a stable cache key, and also stores in-flight asyncio tasks
so concurrent callers asking for the same key share a single network round-trip.
"""

from __future__ import annotations

import asyncio

from chill_out.ecosystems.base import RegistryClient
from chill_out.models import PackageInfo, VersionManifest


class CachingRegistryClient(RegistryClient):
    """
    Wrap a :class:`RegistryClient` with an in-memory dedupe cache.

    The cache lives for the lifetime of the wrapper. Misses delegate to the
    underlying client; concurrent misses for the same key share one fetch.
    """

    def __init__(self, inner: RegistryClient) -> None:
        # Reuse the inner client's HTTP session so a single connection pool
        # serves both layers.
        super().__init__(inner.http)
        self._inner = inner
        self._package_cache: dict[str, PackageInfo | None] = {}
        self._package_inflight: dict[str, asyncio.Task[PackageInfo | None]] = {}
        self._manifest_cache: dict[tuple[str, str], VersionManifest | None] = {}
        self._manifest_inflight: dict[tuple[str, str], asyncio.Task[VersionManifest | None]] = {}

    async def fetch_package(self, name: str) -> PackageInfo | None:
        if name in self._package_cache:
            return self._package_cache[name]
        task = self._package_inflight.get(name)
        if task is None:
            task = asyncio.create_task(self._inner.fetch_package(name))
            self._package_inflight[name] = task
        try:
            result = await task
        finally:
            self._package_inflight.pop(name, None)
        self._package_cache[name] = result
        return result

    async def fetch_version_manifest(self, name: str, version: str) -> VersionManifest | None:
        key = (name, version)
        if key in self._manifest_cache:
            return self._manifest_cache[key]
        task = self._manifest_inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._inner.fetch_version_manifest(name, version))
            self._manifest_inflight[key] = task
        try:
            result = await task
        finally:
            self._manifest_inflight.pop(key, None)
        self._manifest_cache[key] = result
        return result

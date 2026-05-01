"""
Cached front door for registry lookups.

The runner builds one `RegistryClient` per `chill-out` invocation, hands it the
project's `Ecosystem`, and uses it for every package lookup. The client owns
the `httpx.AsyncClient` and an in-memory result cache that also coalesces
concurrent in-flight requests for the same key, so a hundred overlapping
`fetch_package("react")` calls collapse into a single network round-trip.
"""

import asyncio

import httpx

from chill_out.ecosystems.backend import Ecosystem
from chill_out.models import PackageInfo, VersionManifest


class RegistryClient:
    """
    Cached, dedupe-aware wrapper around an `Ecosystem`'s registry methods.

    Result lookups (including `None` results for missing packages) are memoized
    for the lifetime of the instance. Concurrent calls for the same key share
    a single in-flight task so the underlying ecosystem only runs once per key
    no matter how many callers ask for it.
    """

    ecosystem: Ecosystem
    http: httpx.AsyncClient
    package_cache: dict[str, PackageInfo | None]
    package_inflight: dict[str, asyncio.Task[PackageInfo | None]]
    manifest_cache: dict[tuple[str, str], VersionManifest | None]
    manifest_inflight: dict[tuple[str, str], asyncio.Task[VersionManifest | None]]

    def __init__(self, ecosystem: Ecosystem, http: httpx.AsyncClient) -> None:
        """
        Bind an ecosystem to an HTTP session and start with an empty cache.

        Args:
            ecosystem: The ecosystem backend whose `fetch_package` and
                       `fetch_version_manifest` methods will be called on cache miss.
            http:      HTTP session passed through to the ecosystem on every miss.
                       Owned by the caller; the client does not close it.
        """
        self.ecosystem = ecosystem
        self.http = http
        self.package_cache = {}
        self.package_inflight = {}
        self.manifest_cache = {}
        self.manifest_inflight = {}

    async def fetch_package(self, name: str) -> PackageInfo | None:
        """Return release info for `name`, going to the registry only on a cache miss."""
        if name in self.package_cache:
            return self.package_cache[name]

        task = self.package_inflight.get(name)
        if task is None:
            task = asyncio.create_task(self.ecosystem.fetch_package(name, self.http))
            self.package_inflight[name] = task

        try:
            result = await task
        finally:
            self.package_inflight.pop(name, None)

        self.package_cache[name] = result
        return result

    async def fetch_version_manifest(self, name: str, version: str) -> VersionManifest | None:
        """Return the dependency declarations for `(name, version)`, cached after the first call."""
        key = (name, version)
        if key in self.manifest_cache:
            return self.manifest_cache[key]

        task = self.manifest_inflight.get(key)
        if task is None:
            task = asyncio.create_task(self.ecosystem.fetch_version_manifest(name, version, self.http))
            self.manifest_inflight[key] = task

        try:
            result = await task
        finally:
            self.manifest_inflight.pop(key, None)

        self.manifest_cache[key] = result
        return result

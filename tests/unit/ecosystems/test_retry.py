"""Unit tests for the registry-fetch retry behavior in `chill_out.ecosystems.retry`.

Backoff is neutralized for the test suite (see `_instant_registry_retries` in `tests/conftest.py`),
so these tests exercise only the retry decision logic, not the timing. Both ecosystems wrap
`http.get` calls with the same retry policy, so coverage is split across npm and pypi to confirm
the behavior is uniform.
"""

from pathlib import Path

import httpx
import pytest
import respx
from chill_out.ecosystems.constants import NPM_REGISTRY, PYPI_REGISTRY
from chill_out.ecosystems.npm.backend import NpmEcosystem
from chill_out.ecosystems.pypi.backend import PypiEcosystem
from chill_out.exceptions import RegistryError


class TestRetryNpm:
    """The npm backend layers the retry policy on top of the npm registry."""

    @respx.mock
    async def test_fetch_recovers_from_transient_5xx(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        """A 5xx on the first attempt yields to a retry that succeeds."""
        good_payload = {
            "name": "left-pad",
            "time": {"1.0.0": "2015-01-01T00:00:00.000Z"},
        }
        route = respx.get(f"{NPM_REGISTRY}/left-pad").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json=good_payload),
            ]
        )
        info = await NpmEcosystem(root=tmp_path).fetch_package("left-pad", http_client)
        assert info is not None
        assert "1.0.0" in info.releases
        assert route.call_count == 2

    @respx.mock
    async def test_fetch_recovers_from_transient_transport_error(
        self, tmp_path: Path, http_client: httpx.AsyncClient
    ) -> None:
        """A transport hiccup on the first attempt yields to a retry that succeeds."""
        good_payload = {"name": "x", "version": "1.0.0", "dependencies": {"a": "^1.0.0"}}
        route = respx.get(f"{NPM_REGISTRY}/x/1.0.0").mock(
            side_effect=[
                httpx.ConnectError("boom"),
                httpx.Response(200, json=good_payload),
            ]
        )
        manifest = await NpmEcosystem(root=tmp_path).fetch_version_manifest("x", "1.0.0", http_client)
        assert manifest is not None
        assert manifest.deps == {"a": "^1.0.0"}
        assert route.call_count == 2

    @respx.mock
    async def test_fetch_gives_up_after_three_attempts(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        """Persistent 5xx exhausts the retry budget and surfaces a `RegistryError`."""
        route = respx.get(f"{NPM_REGISTRY}/x").mock(return_value=httpx.Response(503))
        with pytest.raises(RegistryError, match="503"):
            await NpmEcosystem(root=tmp_path).fetch_package("x", http_client)
        assert route.call_count == 3

    @respx.mock
    async def test_fetch_does_not_retry_on_4xx(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        """A 4xx is a definitive answer; no retry should be attempted."""
        route = respx.get(f"{NPM_REGISTRY}/missing").mock(return_value=httpx.Response(404))
        result = await NpmEcosystem(root=tmp_path).fetch_package("missing", http_client)
        assert result is None
        assert route.call_count == 1


class TestRetryPypi:
    """The pypi backend layers the same retry policy on top of the PyPI JSON API."""

    @respx.mock
    async def test_fetch_recovers_from_transient_5xx(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        """A 5xx on the first attempt yields to a retry that succeeds."""
        good_payload = {"releases": {"1.0.0": [{"upload_time_iso_8601": "2024-01-01T00:00:00Z"}]}}
        route = respx.get(f"{PYPI_REGISTRY}/requests/json").mock(
            side_effect=[
                httpx.Response(502),
                httpx.Response(200, json=good_payload),
            ]
        )
        info = await PypiEcosystem(root=tmp_path).fetch_package("requests", http_client)
        assert info is not None
        assert "1.0.0" in info.releases
        assert route.call_count == 2

    @respx.mock
    async def test_fetch_recovers_from_transient_transport_error(
        self, tmp_path: Path, http_client: httpx.AsyncClient
    ) -> None:
        """A transport hiccup on the first attempt yields to a retry that succeeds."""
        good_payload = {"info": {"requires_dist": ["click>=8"]}}
        route = respx.get(f"{PYPI_REGISTRY}/x/1.0/json").mock(
            side_effect=[
                httpx.ReadTimeout("slow"),
                httpx.Response(200, json=good_payload),
            ]
        )
        manifest = await PypiEcosystem(root=tmp_path).fetch_version_manifest("x", "1.0", http_client)
        assert manifest is not None
        assert manifest.deps == {"click": ">=8"}
        assert route.call_count == 2

    @respx.mock
    async def test_fetch_gives_up_after_three_attempts(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        """Persistent 5xx exhausts the retry budget and surfaces a `RegistryError`."""
        route = respx.get(f"{PYPI_REGISTRY}/x/json").mock(return_value=httpx.Response(500))
        with pytest.raises(RegistryError, match="500"):
            await PypiEcosystem(root=tmp_path).fetch_package("x", http_client)
        assert route.call_count == 3

    @respx.mock
    async def test_fetch_does_not_retry_on_4xx(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        """A 4xx is a definitive answer; no retry should be attempted."""
        route = respx.get(f"{PYPI_REGISTRY}/missing/json").mock(return_value=httpx.Response(404))
        result = await PypiEcosystem(root=tmp_path).fetch_package("missing", http_client)
        assert result is None
        assert route.call_count == 1

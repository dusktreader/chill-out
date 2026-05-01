"""Unit tests for the npm registry response schemas in `chill_out.ecosystems.npm.schemas`.

The npm backend wraps registry responses in Pydantic models. Drift in the wire format
(missing required fields, type mismatches) must surface as a `RegistryError` so chill-out
fails loudly instead of quietly returning empty `PackageInfo` or `VersionManifest` shells.
"""

from pathlib import Path

import httpx
import pytest
import respx
from chill_out.ecosystems.constants import NPM_REGISTRY
from chill_out.ecosystems.npm.backend import NpmEcosystem
from chill_out.exceptions import RegistryError


class TestSchemaValidation:
    """Round-trip the npm wire format through the backend's Pydantic guards."""

    @respx.mock
    async def test_fetch_package_missing_required_field_raises(
        self, tmp_path: Path, http_client: httpx.AsyncClient
    ) -> None:
        """A response missing the required `name` field is rejected."""
        respx.get(f"{NPM_REGISTRY}/x").mock(return_value=httpx.Response(200, json={"time": {}}))
        with pytest.raises(RegistryError, match="unexpected payload shape"):
            await NpmEcosystem(root=tmp_path).fetch_package("x", http_client)

    @respx.mock
    async def test_fetch_package_wrong_type_for_time_raises(
        self, tmp_path: Path, http_client: httpx.AsyncClient
    ) -> None:
        """A `time` value of the wrong type is rejected."""
        respx.get(f"{NPM_REGISTRY}/x").mock(return_value=httpx.Response(200, json={"name": "x", "time": "not a dict"}))
        with pytest.raises(RegistryError, match="unexpected payload shape"):
            await NpmEcosystem(root=tmp_path).fetch_package("x", http_client)

    @respx.mock
    async def test_fetch_package_ignores_extra_fields(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        """Extras in the npm payload are tolerated; chill-out only cares about its slice."""
        respx.get(f"{NPM_REGISTRY}/x").mock(
            return_value=httpx.Response(
                200,
                json={
                    "name": "x",
                    "time": {"1.0.0": "2024-01-01T00:00:00.000Z"},
                    "dist-tags": {"latest": "1.0.0"},
                    "maintainers": ["alice"],
                },
            )
        )
        info = await NpmEcosystem(root=tmp_path).fetch_package("x", http_client)
        assert info is not None
        assert "1.0.0" in info.releases

    @respx.mock
    async def test_fetch_version_manifest_wrong_type_raises(
        self, tmp_path: Path, http_client: httpx.AsyncClient
    ) -> None:
        """A `dependencies` value of the wrong type is rejected."""
        respx.get(f"{NPM_REGISTRY}/foo/2.0.0").mock(
            return_value=httpx.Response(200, json={"dependencies": "not a dict"})
        )
        with pytest.raises(RegistryError, match="unexpected payload shape"):
            await NpmEcosystem(root=tmp_path).fetch_version_manifest("foo", "2.0.0", http_client)

    @respx.mock
    async def test_fetch_version_manifest_missing_optional_fields_ok(
        self, tmp_path: Path, http_client: httpx.AsyncClient
    ) -> None:
        """Both `dependencies` and `peerDependencies` are optional; absent means empty."""
        respx.get(f"{NPM_REGISTRY}/foo/2.0.0").mock(return_value=httpx.Response(200, json={}))
        manifest = await NpmEcosystem(root=tmp_path).fetch_version_manifest("foo", "2.0.0", http_client)
        assert manifest is not None
        assert manifest.deps == {}


class TestYankParsing:
    """Versions present in `time` but missing from `versions` are flagged as yanked.

    npm keeps the publish timestamp in `time` even after `npm unpublish`, but
    drops the per-version document from `versions`. The backend uses the
    set difference as the yank signal, with one defensive twist: an empty or
    absent `versions` map is treated as "yank status unknown" so older
    fixtures and abbreviated registry formats don't get every release
    flagged.
    """

    @respx.mock
    async def test_unpublished_version_marked_yanked(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        """A version in `time` but missing from `versions` is yanked."""
        respx.get(f"{NPM_REGISTRY}/x").mock(
            return_value=httpx.Response(
                200,
                json={
                    "name": "x",
                    "time": {
                        "1.0.0": "2024-01-01T00:00:00.000Z",
                        "1.1.0": "2024-02-01T00:00:00.000Z",
                    },
                    "versions": {
                        "1.0.0": {"name": "x", "version": "1.0.0"},
                    },
                },
            )
        )
        info = await NpmEcosystem(root=tmp_path).fetch_package("x", http_client)
        assert info is not None
        assert info.releases["1.0.0"].yanked is False
        assert info.releases["1.1.0"].yanked is True

    @respx.mock
    async def test_versions_map_absent_treated_as_unknown(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        """No `versions` map means yank status is unknown; nothing is flagged."""
        respx.get(f"{NPM_REGISTRY}/x").mock(
            return_value=httpx.Response(
                200,
                json={
                    "name": "x",
                    "time": {"1.0.0": "2024-01-01T00:00:00.000Z"},
                },
            )
        )
        info = await NpmEcosystem(root=tmp_path).fetch_package("x", http_client)
        assert info is not None
        assert info.releases["1.0.0"].yanked is False

    @respx.mock
    async def test_versions_map_present_and_complete_no_yanks(
        self, tmp_path: Path, http_client: httpx.AsyncClient
    ) -> None:
        """Every release with a matching `versions` entry is not yanked."""
        respx.get(f"{NPM_REGISTRY}/x").mock(
            return_value=httpx.Response(
                200,
                json={
                    "name": "x",
                    "time": {"1.0.0": "2024-01-01T00:00:00.000Z"},
                    "versions": {"1.0.0": {"name": "x", "version": "1.0.0"}},
                },
            )
        )
        info = await NpmEcosystem(root=tmp_path).fetch_package("x", http_client)
        assert info is not None
        assert info.releases["1.0.0"].yanked is False

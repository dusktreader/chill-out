"""Unit tests for the pypi registry response schemas in `chill_out.ecosystems.pypi.schemas`.

The pypi backend wraps registry responses in Pydantic models. Drift in the wire format
(missing required fields, type mismatches, unparsable `requires_dist` rows) must surface as
a `RegistryError` so chill-out fails loudly instead of quietly returning empty `PackageInfo`
or `VersionManifest` shells.
"""

from pathlib import Path

import httpx
import pytest
import respx
from chill_out.ecosystems.constants import PYPI_REGISTRY
from chill_out.ecosystems.pypi.backend import PypiEcosystem
from chill_out.exceptions import RegistryError


class TestSchemaValidation:
    """Round-trip the pypi wire format through the backend's Pydantic guards."""

    @respx.mock
    async def test_fetch_package_wrong_type_for_releases_raises(
        self, tmp_path: Path, http_client: httpx.AsyncClient
    ) -> None:
        """A `releases` value of the wrong type is rejected."""
        respx.get(f"{PYPI_REGISTRY}/x/json").mock(return_value=httpx.Response(200, json={"releases": "not a dict"}))
        with pytest.raises(RegistryError, match="unexpected payload shape"):
            await PypiEcosystem(root=tmp_path).fetch_package("x", http_client)

    @respx.mock
    async def test_fetch_package_unparsable_timestamp_raises(
        self, tmp_path: Path, http_client: httpx.AsyncClient
    ) -> None:
        """An artifact timestamp that doesn't parse as ISO-8601 is rejected."""
        respx.get(f"{PYPI_REGISTRY}/x/json").mock(
            return_value=httpx.Response(
                200,
                json={"releases": {"1.0": [{"upload_time_iso_8601": "totally not a date"}]}},
            )
        )
        with pytest.raises(RegistryError, match="unexpected payload shape"):
            await PypiEcosystem(root=tmp_path).fetch_package("x", http_client)

    @respx.mock
    async def test_fetch_package_ignores_extra_artifact_fields(
        self, tmp_path: Path, http_client: httpx.AsyncClient
    ) -> None:
        """PyPI returns many artifact fields (size, url, sha256, ...); only timestamps matter."""
        respx.get(f"{PYPI_REGISTRY}/x/json").mock(
            return_value=httpx.Response(
                200,
                json={
                    "releases": {
                        "1.0": [
                            {
                                "upload_time_iso_8601": "2024-01-01T00:00:00.000Z",
                                "url": "https://files.pythonhosted.org/...",
                                "size": 12345,
                                "sha256": "deadbeef",
                            }
                        ]
                    }
                },
            )
        )
        info = await PypiEcosystem(root=tmp_path).fetch_package("x", http_client)
        assert info is not None
        assert "1.0" in info.releases

    @respx.mock
    async def test_fetch_package_artifact_without_timestamp_raises(
        self, tmp_path: Path, http_client: httpx.AsyncClient
    ) -> None:
        """An artifact with neither upload_time field surfaces as a `RegistryError`.

        Strict-throughout semantics: a payload that strips every timestamp from an artifact
        is treated as drift in the registry's response shape rather than silently dropped.
        """
        respx.get(f"{PYPI_REGISTRY}/x/json").mock(
            return_value=httpx.Response(
                200,
                json={
                    "releases": {
                        "1.0": [{}],
                        "2.0": [{"upload_time_iso_8601": "2024-01-01T00:00:00.000Z"}],
                    }
                },
            )
        )
        with pytest.raises(RegistryError, match="unexpected payload shape"):
            await PypiEcosystem(root=tmp_path).fetch_package("x", http_client)

    @respx.mock
    async def test_fetch_package_skips_releases_with_no_artifacts(
        self, tmp_path: Path, http_client: httpx.AsyncClient
    ) -> None:
        """A release with an empty artifact list is dropped from the result.

        Fully-deleted releases (every artifact removed by an admin or by a
        deletion request, not a yank) come back with an empty list. That
        shape is wire-valid and produces no `PackageRelease`. Yanked releases
        are different: they keep their artifacts and arrive with `yanked:
        true` per file. See the yank tests below.
        """
        respx.get(f"{PYPI_REGISTRY}/x/json").mock(
            return_value=httpx.Response(
                200,
                json={
                    "releases": {
                        "1.0": [],
                        "2.0": [{"upload_time_iso_8601": "2024-01-01T00:00:00.000Z"}],
                    }
                },
            )
        )
        info = await PypiEcosystem(root=tmp_path).fetch_package("x", http_client)
        assert info is not None
        assert "1.0" not in info.releases
        assert "2.0" in info.releases

    @respx.mock
    async def test_fetch_version_manifest_missing_info_ok(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        """The `info` block defaults to empty when absent; no requires_dist means no deps."""
        respx.get(f"{PYPI_REGISTRY}/foo/1.0/json").mock(return_value=httpx.Response(200, json={}))
        manifest = await PypiEcosystem(root=tmp_path).fetch_version_manifest("foo", "1.0", http_client)
        assert manifest is not None
        assert manifest.deps == {}

    @respx.mock
    async def test_fetch_version_manifest_wrong_type_raises(
        self, tmp_path: Path, http_client: httpx.AsyncClient
    ) -> None:
        """A `requires_dist` value of the wrong type is rejected."""
        respx.get(f"{PYPI_REGISTRY}/foo/1.0/json").mock(
            return_value=httpx.Response(200, json={"info": {"requires_dist": "not a list"}})
        )
        with pytest.raises(RegistryError, match="unexpected payload shape"):
            await PypiEcosystem(root=tmp_path).fetch_version_manifest("foo", "1.0", http_client)

    @respx.mock
    async def test_fetch_version_manifest_explicit_null_requires_dist_ok(
        self, tmp_path: Path, http_client: httpx.AsyncClient
    ) -> None:
        """An explicit `null` for `requires_dist` runs the validator and is accepted as 'no deps'."""
        respx.get(f"{PYPI_REGISTRY}/foo/1.0/json").mock(
            return_value=httpx.Response(200, json={"info": {"requires_dist": None}})
        )
        manifest = await PypiEcosystem(root=tmp_path).fetch_version_manifest("foo", "1.0", http_client)
        assert manifest is not None
        assert manifest.deps == {}


class TestYankParsing:
    """Per-artifact `yanked` flags collapse to a per-release `yanked` field on `PackageRelease`."""

    @respx.mock
    async def test_release_yanked_when_every_artifact_yanked(
        self, tmp_path: Path, http_client: httpx.AsyncClient
    ) -> None:
        """Every artifact marked yanked makes the release yanked overall."""
        respx.get(f"{PYPI_REGISTRY}/x/json").mock(
            return_value=httpx.Response(
                200,
                json={
                    "releases": {
                        "1.0": [
                            {"upload_time_iso_8601": "2024-01-01T00:00:00.000Z", "yanked": True},
                            {"upload_time_iso_8601": "2024-01-01T00:00:00.000Z", "yanked": True},
                        ],
                    }
                },
            )
        )
        info = await PypiEcosystem(root=tmp_path).fetch_package("x", http_client)
        assert info is not None
        assert info.releases["1.0"].yanked is True

    @respx.mock
    async def test_release_not_yanked_when_any_artifact_clean(
        self, tmp_path: Path, http_client: httpx.AsyncClient
    ) -> None:
        """A release with at least one non-yanked artifact is still installable, so not yanked.

        Mirrors `pip`/`uv` resolver behavior: as long as one file is live the
        release is reachable.
        """
        respx.get(f"{PYPI_REGISTRY}/x/json").mock(
            return_value=httpx.Response(
                200,
                json={
                    "releases": {
                        "1.0": [
                            {"upload_time_iso_8601": "2024-01-01T00:00:00.000Z", "yanked": True},
                            {"upload_time_iso_8601": "2024-01-01T00:00:00.000Z", "yanked": False},
                        ],
                    }
                },
            )
        )
        info = await PypiEcosystem(root=tmp_path).fetch_package("x", http_client)
        assert info is not None
        assert info.releases["1.0"].yanked is False

    @respx.mock
    async def test_release_not_yanked_when_field_absent(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        """Older artifacts predate the yank field; missing means not yanked."""
        respx.get(f"{PYPI_REGISTRY}/x/json").mock(
            return_value=httpx.Response(
                200,
                json={
                    "releases": {
                        "1.0": [{"upload_time_iso_8601": "2024-01-01T00:00:00.000Z"}],
                    }
                },
            )
        )
        info = await PypiEcosystem(root=tmp_path).fetch_package("x", http_client)
        assert info is not None
        assert info.releases["1.0"].yanked is False

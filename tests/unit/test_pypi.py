"""Unit tests for the pypi ecosystem backend (no real pypi calls, no real uv)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx
from chill_out.constants import EcosystemKind
from chill_out.ecosystems.pypi import PYPI_REGISTRY, PypiEcosystem, PypiRegistryClient, _normalize
from chill_out.exceptions import EcosystemError, RegistryError
from chill_out.models import FixAction


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as client:
        yield client


# ---------------------------------------------------------------------------
# PypiRegistryClient
# ---------------------------------------------------------------------------


class TestPypiRegistryClient:
    @respx.mock
    async def test_fetch_returns_earliest_upload_per_version(self, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{PYPI_REGISTRY}/requests/json").mock(
            return_value=httpx.Response(
                200,
                json={
                    "releases": {
                        "2.31.0": [
                            {"upload_time_iso_8601": "2023-05-22T15:12:00.000Z"},
                            {"upload_time_iso_8601": "2023-05-22T15:00:00.000Z"},
                        ],
                        "2.30.0": [{"upload_time_iso_8601": "2023-04-01T00:00:00.000Z"}],
                        "empty": [],
                    }
                },
            )
        )
        client = PypiRegistryClient(http_client)
        info = await client.fetch_package("requests")
        assert info is not None
        assert "empty" not in info.releases
        published = info.published_at("2.31.0")
        assert published is not None
        assert published.to_iso8601_string().startswith("2023-05-22T15:00:00")

    @respx.mock
    async def test_404_returns_none(self, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{PYPI_REGISTRY}/nope/json").mock(return_value=httpx.Response(404))
        assert await PypiRegistryClient(http_client).fetch_package("nope") is None

    @respx.mock
    async def test_5xx_raises(self, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{PYPI_REGISTRY}/x/json").mock(return_value=httpx.Response(500))
        with pytest.raises(RegistryError):
            await PypiRegistryClient(http_client).fetch_package("x")

    @respx.mock
    async def test_transport_error_raises(self, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{PYPI_REGISTRY}/x/json").mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(RegistryError, match="transport error"):
            await PypiRegistryClient(http_client).fetch_package("x")


# ---------------------------------------------------------------------------
# PypiEcosystem
# ---------------------------------------------------------------------------


class TestPypiEcosystemDetect:
    def test_true_with_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n")
        assert PypiEcosystem.detect(tmp_path) is True

    def test_false_without(self, tmp_path: Path) -> None:
        assert PypiEcosystem.detect(tmp_path) is False


class TestPypiEcosystemLoadDirect:
    def test_loads_from_lock(self, pypi_project: Path) -> None:
        eco = PypiEcosystem(pypi_project)
        pkgs = eco.load_installed(deep=False)
        names = {p.name: p.version for p in pkgs}
        assert names == {"requests": "2.31.0", "click": "8.1.7"}
        for p in pkgs:
            assert p.ecosystem is EcosystemKind.PYPI

    def test_falls_back_to_pinned_spec_when_no_lock(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="x"\nversion="0"\ndependencies = ["only-pinned==1.2.3", "no-pin>=1"]\n'
        )
        eco = PypiEcosystem(tmp_path)
        pkgs = eco.load_installed(deep=False)
        names = {p.name: p.version for p in pkgs}
        assert names == {"only-pinned": "1.2.3"}

    def test_includes_optional_and_dev_groups(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="x"\nversion="0"\n'
            'dependencies = ["a==1"]\n'
            "[project.optional-dependencies]\n"
            'extra = ["b==2"]\n'
            "[dependency-groups]\n"
            'dev = ["c==3"]\n'
        )
        eco = PypiEcosystem(tmp_path)
        pkgs = eco.load_installed(deep=False)
        assert {p.name for p in pkgs} == {"a", "b", "c"}


class TestPypiEcosystemLoadDeep:
    def test_loads_all_with_via_chains(self, pypi_project: Path) -> None:
        eco = PypiEcosystem(pypi_project)
        pkgs = eco.load_installed(deep=True)
        by_name = {p.name: p for p in pkgs}
        assert "requests" in by_name and by_name["requests"].via is None
        # urllib3 is in lock as a transitive entry; with no link it gets empty via chain
        assert "urllib3" in by_name

    def test_raises_without_lock(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion="0"\ndependencies=["a==1"]\n')
        eco = PypiEcosystem(tmp_path)
        with pytest.raises(EcosystemError, match="uv.lock"):
            eco.load_installed(deep=True)


class TestPypiEcosystemApplyFixes:
    def test_pins_existing_dependency(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion="0"\ndependencies = ["requests>=2.0"]\n')
        eco = PypiEcosystem(tmp_path)
        fake = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("chill_out.ecosystems.pypi.subprocess.run", return_value=fake):
            log = eco.apply_fixes([FixAction(package="requests", version="2.30.0")])
        contents = (tmp_path / "pyproject.toml").read_text()
        assert "requests==2.30.0" in contents
        assert any("pinned requests" in line for line in log)
        assert "ran: uv lock" in log

    def test_adds_when_missing(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion="0"\ndependencies = []\n')
        eco = PypiEcosystem(tmp_path)
        fake = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("chill_out.ecosystems.pypi.subprocess.run", return_value=fake):
            log = eco.apply_fixes([FixAction(package="newpkg", version="1.2.3")])
        contents = (tmp_path / "pyproject.toml").read_text()
        assert "newpkg==1.2.3" in contents
        assert any("added newpkg" in line for line in log)

    def test_uv_lock_failure_raises(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion="0"\ndependencies = ["a==1"]\n')
        eco = PypiEcosystem(tmp_path)
        fake = type("R", (), {"returncode": 1, "stdout": "", "stderr": "lock failed"})()
        with patch("chill_out.ecosystems.pypi.subprocess.run", return_value=fake):
            with pytest.raises(EcosystemError, match="lock failed"):
                eco.apply_fixes([FixAction(package="a", version="0.9.0")])

    def test_empty_actions_noop(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion="0"\n')
        eco = PypiEcosystem(tmp_path)
        assert eco.apply_fixes([]) == []


class TestNormalize:
    def test_lowercases_and_collapses_separators(self) -> None:
        assert _normalize("Foo_Bar.baz--qux") == "foo-bar-baz-qux"


class TestPypiFetchVersionManifest:
    @pytest.fixture
    async def http_client(self):
        async with httpx.AsyncClient() as client:
            yield client

    @respx.mock
    async def test_returns_requires_dist_specifiers(self, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{PYPI_REGISTRY}/requests/2.31.0/json").mock(
            return_value=httpx.Response(
                200,
                json={
                    "info": {
                        "requires_dist": [
                            "urllib3>=1.21.1,<3",
                            "certifi>=2017.4.17",
                        ]
                    }
                },
            )
        )
        client = PypiRegistryClient(http_client)
        manifest = await client.fetch_version_manifest("requests", "2.31.0")
        assert manifest is not None
        assert set(manifest.deps) == {"urllib3", "certifi"}
        assert "1.21.1" in manifest.deps["urllib3"]
        assert "2017.4.17" in manifest.deps["certifi"]

    @respx.mock
    async def test_skips_extra_marker_requirements(self, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{PYPI_REGISTRY}/requests/2.31.0/json").mock(
            return_value=httpx.Response(
                200,
                json={
                    "info": {
                        "requires_dist": [
                            "urllib3>=1.21.1",
                            "pysocks>=1.5.6; extra == 'socks'",
                        ]
                    }
                },
            )
        )
        client = PypiRegistryClient(http_client)
        manifest = await client.fetch_version_manifest("requests", "2.31.0")
        assert manifest is not None
        assert "urllib3" in manifest.deps
        assert "pysocks" not in manifest.deps

    @respx.mock
    async def test_skips_unparsable_entries(self, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{PYPI_REGISTRY}/foo/1.0/json").mock(
            return_value=httpx.Response(
                200,
                json={"info": {"requires_dist": ["valid>=1", "this is garbage"]}},
            )
        )
        client = PypiRegistryClient(http_client)
        manifest = await client.fetch_version_manifest("foo", "1.0")
        assert manifest is not None
        assert manifest.deps == {"valid": ">=1"}

    @respx.mock
    async def test_404_returns_none(self, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{PYPI_REGISTRY}/foo/1.0/json").mock(return_value=httpx.Response(404))
        client = PypiRegistryClient(http_client)
        assert await client.fetch_version_manifest("foo", "1.0") is None

    @respx.mock
    async def test_500_raises(self, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{PYPI_REGISTRY}/foo/1.0/json").mock(return_value=httpx.Response(500))
        client = PypiRegistryClient(http_client)
        with pytest.raises(RegistryError):
            await client.fetch_version_manifest("foo", "1.0")

    @respx.mock
    async def test_transport_error_raises(self, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{PYPI_REGISTRY}/foo/1.0/json").mock(side_effect=httpx.ConnectError("boom"))
        client = PypiRegistryClient(http_client)
        with pytest.raises(RegistryError):
            await client.fetch_version_manifest("foo", "1.0")

    @respx.mock
    async def test_non_json_raises(self, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{PYPI_REGISTRY}/foo/1.0/json").mock(return_value=httpx.Response(200, content=b"not json"))
        client = PypiRegistryClient(http_client)
        with pytest.raises(RegistryError):
            await client.fetch_version_manifest("foo", "1.0")


class TestPypiRangeSatisfies:
    def _eco(self, tmp_path: Path) -> PypiEcosystem:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0.1'\n")
        return PypiEcosystem(tmp_path)

    def test_admits_version_in_range(self, tmp_path: Path) -> None:
        eco = self._eco(tmp_path)
        assert eco.range_satisfies("1.5.0", ">=1.0,<2.0") is True

    def test_rejects_version_outside_range(self, tmp_path: Path) -> None:
        eco = self._eco(tmp_path)
        assert eco.range_satisfies("2.5.0", ">=1.0,<2.0") is False

    def test_empty_range_admits_anything(self, tmp_path: Path) -> None:
        eco = self._eco(tmp_path)
        assert eco.range_satisfies("99.99.99", "") is True

    def test_unparsable_version_falls_back_permissive(self, tmp_path: Path) -> None:
        eco = self._eco(tmp_path)
        assert eco.range_satisfies("not-a-version", ">=1.0") is True

    def test_unparsable_specifier_falls_back_permissive(self, tmp_path: Path) -> None:
        eco = self._eco(tmp_path)
        assert eco.range_satisfies("1.0.0", "not a specifier") is True

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
    async def test_fetch_returns_earliest_upload_per_version(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.get(f"{PYPI_REGISTRY}/requests/json").mock(
            return_value=httpx.Response(
                200,
                json={
                    "releases": {
                        "2.31.0": [
                            {"upload_time_iso_8601": "2023-05-22T15:12:00.000Z"},
                            {"upload_time_iso_8601": "2023-05-22T15:00:00.000Z"},
                        ],
                        "2.30.0": [
                            {"upload_time_iso_8601": "2023-04-01T00:00:00.000Z"}
                        ],
                        "empty": [],
                    }
                },
            )
        )
        client = PypiRegistryClient(http_client)
        info = await client.fetch_package("requests")
        assert info is not None
        assert "empty" not in info.releases
        assert info.published_at("2.31.0") is not None
        assert info.published_at("2.31.0").to_iso8601_string().startswith("2023-05-22T15:00:00")

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
            '[project]\nname="x"\nversion="0"\n'
            'dependencies = ["only-pinned==1.2.3", "no-pin>=1"]\n'
        )
        eco = PypiEcosystem(tmp_path)
        pkgs = eco.load_installed(deep=False)
        names = {p.name: p.version for p in pkgs}
        assert names == {"only-pinned": "1.2.3"}

    def test_includes_optional_and_dev_groups(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="x"\nversion="0"\n'
            'dependencies = ["a==1"]\n'
            '[project.optional-dependencies]\n'
            'extra = ["b==2"]\n'
            '[dependency-groups]\n'
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
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="x"\nversion="0"\ndependencies = ["requests>=2.0"]\n'
        )
        eco = PypiEcosystem(tmp_path)
        fake = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("chill_out.ecosystems.pypi.subprocess.run", return_value=fake):
            log = eco.apply_fixes([FixAction(package="requests", version="2.30.0")])
        contents = (tmp_path / "pyproject.toml").read_text()
        assert "requests==2.30.0" in contents
        assert any("pinned requests" in line for line in log)
        assert "ran: uv lock" in log

    def test_adds_when_missing(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="x"\nversion="0"\ndependencies = []\n'
        )
        eco = PypiEcosystem(tmp_path)
        fake = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("chill_out.ecosystems.pypi.subprocess.run", return_value=fake):
            log = eco.apply_fixes([FixAction(package="newpkg", version="1.2.3")])
        contents = (tmp_path / "pyproject.toml").read_text()
        assert "newpkg==1.2.3" in contents
        assert any("added newpkg" in line for line in log)

    def test_uv_lock_failure_raises(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="x"\nversion="0"\ndependencies = ["a==1"]\n'
        )
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

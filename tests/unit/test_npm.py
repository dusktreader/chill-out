"""Unit tests for the npm ecosystem backend (no real npm calls)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx
from chill_out.ecosystems.npm import NPM_REGISTRY, NpmEcosystem, NpmRegistryClient
from chill_out.exceptions import EcosystemError, RegistryError

# ---------------------------------------------------------------------------
# NpmRegistryClient
# ---------------------------------------------------------------------------


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as client:
        yield client


class TestNpmRegistryClient:
    @respx.mock
    async def test_fetch_returns_releases(self, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{NPM_REGISTRY}/left-pad").mock(
            return_value=httpx.Response(
                200,
                json={
                    "name": "left-pad",
                    "time": {
                        "created": "2014-01-01T00:00:00.000Z",
                        "modified": "2024-01-01T00:00:00.000Z",
                        "1.0.0": "2015-01-01T00:00:00.000Z",
                        "1.3.0": "2016-03-22T00:00:00.000Z",
                    },
                },
            )
        )
        client = NpmRegistryClient(http_client)
        info = await client.fetch_package("left-pad")
        assert info is not None
        assert set(info.releases) == {"1.0.0", "1.3.0"}
        assert info.published_at("1.3.0") is not None

    @respx.mock
    async def test_fetch_missing_returns_none(self, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{NPM_REGISTRY}/nope").mock(return_value=httpx.Response(404))
        client = NpmRegistryClient(http_client)
        assert await client.fetch_package("nope") is None

    @respx.mock
    async def test_fetch_5xx_raises(self, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{NPM_REGISTRY}/x").mock(return_value=httpx.Response(503))
        client = NpmRegistryClient(http_client)
        with pytest.raises(RegistryError, match="503"):
            await client.fetch_package("x")

    @respx.mock
    async def test_fetch_transport_error_raises(self, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{NPM_REGISTRY}/x").mock(side_effect=httpx.ConnectError("boom"))
        client = NpmRegistryClient(http_client)
        with pytest.raises(RegistryError, match="transport error"):
            await client.fetch_package("x")

    @respx.mock
    async def test_fetch_ignores_unparsable_dates(self, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{NPM_REGISTRY}/x").mock(
            return_value=httpx.Response(200, json={"time": {"1.0.0": "garbage", "2.0.0": "2024-01-01T00:00:00.000Z"}})
        )
        client = NpmRegistryClient(http_client)
        info = await client.fetch_package("x")
        assert info is not None
        assert "1.0.0" not in info.releases
        assert "2.0.0" in info.releases


# ---------------------------------------------------------------------------
# NpmEcosystem
# ---------------------------------------------------------------------------


def _write_pkg_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


class TestNpmEcosystemDetect:
    def test_true_with_package_json(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "x"})
        assert NpmEcosystem.detect(tmp_path) is True

    def test_false_without_package_json(self, tmp_path: Path) -> None:
        assert NpmEcosystem.detect(tmp_path) is False


class TestNpmEcosystemLoadDirect:
    def test_filters_to_declared_deps(self, tmp_path: Path) -> None:
        _write_pkg_json(
            tmp_path / "package.json",
            {"name": "app", "dependencies": {"left-pad": "^1.0.0"}},
        )
        eco = NpmEcosystem(tmp_path)
        fake_npm_list = {
            "dependencies": {
                "left-pad": {"version": "1.3.0"},
                "extra-pkg": {"version": "9.9.9"},  # not declared
            }
        }
        with patch.object(NpmEcosystem, "_npm_list", return_value=fake_npm_list):
            pkgs = eco.load_installed(deep=False)
        names = {p.name for p in pkgs}
        assert names == {"left-pad"}

    def test_skips_file_resolved(self, tmp_path: Path) -> None:
        _write_pkg_json(
            tmp_path / "package.json",
            {"name": "app", "dependencies": {"local-thing": "file:../local-thing"}},
        )
        eco = NpmEcosystem(tmp_path)
        fake = {
            "dependencies": {
                "local-thing": {"version": "1.0.0", "resolved": "file:../local-thing"},
            }
        }
        with patch.object(NpmEcosystem, "_npm_list", return_value=fake):
            assert eco.load_installed(deep=False) == []


class TestNpmEcosystemNpmList:
    def test_raises_on_unexpected_exit(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "x"})
        eco = NpmEcosystem(tmp_path)
        fake_result = type("R", (), {"returncode": 99, "stdout": "", "stderr": "kaboom"})()
        with patch("chill_out.ecosystems.npm.subprocess.run", return_value=fake_result):
            with pytest.raises(EcosystemError, match="kaboom"):
                eco._npm_list(depth=1)

    def test_accepts_exit_code_1(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "x"})
        eco = NpmEcosystem(tmp_path)
        fake_result = type("R", (), {"returncode": 1, "stdout": '{"dependencies": {}}', "stderr": ""})()
        with patch("chill_out.ecosystems.npm.subprocess.run", return_value=fake_result):
            assert eco._npm_list(depth=1) == {"dependencies": {}}


class TestNpmEcosystemApplyFixes:
    def test_writes_overrides_and_runs_install(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "app", "dependencies": {"left-pad": "^1.0.0"}})
        eco = NpmEcosystem(tmp_path)
        from chill_out.models import FixAction

        fake_install = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("chill_out.ecosystems.npm.subprocess.run", return_value=fake_install):
            log = eco.apply_fixes([FixAction(package="left-pad", version="1.2.0", is_override=True)])
        new_pkg = json.loads((tmp_path / "package.json").read_text())
        assert new_pkg["overrides"] == {"left-pad": "1.2.0"}
        assert any("override" in line for line in log)
        assert "ran: npm install" in log

    def test_install_failure_raises(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "app"})
        eco = NpmEcosystem(tmp_path)
        from chill_out.models import FixAction

        fake_install = type("R", (), {"returncode": 1, "stdout": "", "stderr": "ENOSOLO"})()
        with patch("chill_out.ecosystems.npm.subprocess.run", return_value=fake_install):
            with pytest.raises(EcosystemError, match="ENOSOLO"):
                eco.apply_fixes([FixAction(package="x", version="1.0.0")])

    def test_empty_actions_noop(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "app"})
        eco = NpmEcosystem(tmp_path)
        assert eco.apply_fixes([]) == []


class TestNpmEcosystemLoadDeep:
    def test_attributes_transitives_to_principals(self, tmp_path: Path) -> None:
        _write_pkg_json(
            tmp_path / "package.json",
            {"name": "app", "dependencies": {"principal": "^1.0.0"}},
        )
        # package-lock with a dep graph: principal -> middle -> leaf
        (tmp_path / "package-lock.json").write_text(
            json.dumps(
                {
                    "packages": {
                        "node_modules/principal": {"dependencies": {"middle": "^1"}},
                        "node_modules/middle": {"dependencies": {"leaf": "^1"}},
                        "node_modules/leaf": {},
                    }
                }
            )
        )
        npm_list = {
            "dependencies": {
                "principal": {
                    "version": "1.0.0",
                    "dependencies": {
                        "middle": {
                            "version": "1.0.0",
                            "dependencies": {"leaf": {"version": "1.0.0"}},
                        }
                    },
                }
            }
        }
        eco = NpmEcosystem(tmp_path)
        with patch.object(NpmEcosystem, "_npm_list", return_value=npm_list):
            pkgs = eco.load_installed(deep=True)
        by_name = {p.name: p for p in pkgs}
        assert by_name["principal"].via is None
        assert by_name["leaf"].via == "principal"
        assert by_name["leaf"].via_chain == ("middle", "principal")

    def test_unparsable_lock_yields_empty_graph(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "app", "dependencies": {"x": "^1"}})
        (tmp_path / "package-lock.json").write_text("not json")
        eco = NpmEcosystem(tmp_path)
        with patch.object(
            NpmEcosystem,
            "_npm_list",
            return_value={"dependencies": {"x": {"version": "1.0.0"}}},
        ):
            pkgs = eco.load_installed(deep=True)
        assert {p.name for p in pkgs} == {"x"}

    def test_unparsable_package_json_is_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("garbage")
        # Add a valid sub-package.json so we don't hit the "no deps declared" code path entirely.
        sub = tmp_path / "sub"
        sub.mkdir()
        _write_pkg_json(sub / "package.json", {"name": "sub", "dependencies": {"x": "^1"}})
        eco = NpmEcosystem(tmp_path)
        with patch.object(
            NpmEcosystem,
            "_npm_list",
            return_value={
                "dependencies": {
                    "sub": {
                        "version": "0.0.0",
                        "resolved": "file:./sub",
                        "name": "sub",
                        "dependencies": {"x": {"version": "1.0.0"}},
                    }
                }
            },
        ):
            pkgs = eco.load_installed(deep=False)
        assert {p.name for p in pkgs} == {"x"}


class TestNpmFetchVersionManifest:
    @respx.mock
    async def test_returns_merged_dependencies(self, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{NPM_REGISTRY}/foo/2.0.0").mock(
            return_value=httpx.Response(
                200,
                json={
                    "dependencies": {"bar": "^1.0.0"},
                    "peerDependencies": {"baz": ">=2.0"},
                },
            )
        )
        client = NpmRegistryClient(http_client)
        manifest = await client.fetch_version_manifest("foo", "2.0.0")
        assert manifest is not None
        assert manifest.deps == {"bar": "^1.0.0", "baz": ">=2.0"}

    @respx.mock
    async def test_404_returns_none(self, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{NPM_REGISTRY}/foo/2.0.0").mock(return_value=httpx.Response(404))
        client = NpmRegistryClient(http_client)
        assert await client.fetch_version_manifest("foo", "2.0.0") is None

    @respx.mock
    async def test_500_raises(self, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{NPM_REGISTRY}/foo/2.0.0").mock(return_value=httpx.Response(500))
        client = NpmRegistryClient(http_client)
        with pytest.raises(RegistryError):
            await client.fetch_version_manifest("foo", "2.0.0")

    @respx.mock
    async def test_transport_error_raises(self, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{NPM_REGISTRY}/foo/2.0.0").mock(side_effect=httpx.ConnectError("boom"))
        client = NpmRegistryClient(http_client)
        with pytest.raises(RegistryError):
            await client.fetch_version_manifest("foo", "2.0.0")

    @respx.mock
    async def test_non_json_raises(self, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{NPM_REGISTRY}/foo/2.0.0").mock(return_value=httpx.Response(200, content=b"not json"))
        client = NpmRegistryClient(http_client)
        with pytest.raises(RegistryError):
            await client.fetch_version_manifest("foo", "2.0.0")


class TestNpmRangeSatisfies:
    """The npm range check shells out to node; patch subprocess.run to keep tests hermetic."""

    def _eco(self, tmp_path: Path) -> NpmEcosystem:
        (tmp_path / "package.json").write_text("{}")
        return NpmEcosystem(tmp_path)

    def test_returns_true_on_node_exit_zero(self, tmp_path: Path) -> None:
        eco = self._eco(tmp_path)
        with patch("chill_out.ecosystems.npm.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            assert eco.range_satisfies("1.5.0", "^1.0.0") is True

    def test_returns_false_on_node_exit_one(self, tmp_path: Path) -> None:
        eco = self._eco(tmp_path)
        with patch("chill_out.ecosystems.npm.subprocess.run") as run:
            run.return_value.returncode = 1
            run.return_value.stderr = ""
            assert eco.range_satisfies("2.0.0", "^1.0.0") is False

    def test_falls_back_permissive_when_node_missing(self, tmp_path: Path) -> None:
        eco = self._eco(tmp_path)
        with patch("chill_out.ecosystems.npm.subprocess.run", side_effect=FileNotFoundError):
            assert eco.range_satisfies("1.5.0", "^1.0.0") is True

    def test_falls_back_permissive_on_unexpected_exit(self, tmp_path: Path) -> None:
        eco = self._eco(tmp_path)
        with patch("chill_out.ecosystems.npm.subprocess.run") as run:
            run.return_value.returncode = 2
            run.return_value.stderr = "no semver module"
            assert eco.range_satisfies("1.5.0", "^1.0.0") is True

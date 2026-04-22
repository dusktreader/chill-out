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

    def test_descends_into_workspace_member_to_find_declared_deps(self, tmp_path: Path) -> None:
        # When chill-out runs inside a workspace member, `npm list` walks up to
        # the workspace root and reports the member as a file:-resolved entry
        # with the member's actual deps nested one level deeper. The backend
        # has to descend into that node to match against the local
        # package.json's declared deps.
        _write_pkg_json(
            tmp_path / "package.json",
            {"name": "api", "dependencies": {"left-pad": "^1.0.0", "right-pad": "^2.0.0"}},
        )
        eco = NpmEcosystem(tmp_path)
        fake_npm_list = {
            "dependencies": {
                "@workspace/api": {
                    "version": "1.0.0",
                    "resolved": "file:../../api",
                    "dependencies": {
                        "left-pad": {"version": "1.3.0"},
                        "right-pad": {"version": "2.5.0"},
                        "untracked-transitive": {"version": "9.9.9"},
                    },
                },
            }
        }
        with patch.object(NpmEcosystem, "_npm_list", return_value=fake_npm_list):
            pkgs = eco.load_installed(deep=False)
        names = {p.name for p in pkgs}
        # Both declared deps come back; the unrelated transitive does not, and
        # the workspace member itself is never reported.
        assert names == {"left-pad", "right-pad"}

    def test_does_not_descend_through_nested_workspace_members(self, tmp_path: Path) -> None:
        # Only the first-level descent into a file:-resolved node should
        # happen; deeper nesting is the resolver's transitive territory and
        # belongs in --deep, not direct mode.
        _write_pkg_json(
            tmp_path / "package.json",
            {"name": "api", "dependencies": {"left-pad": "^1"}},
        )
        eco = NpmEcosystem(tmp_path)
        fake_npm_list = {
            "dependencies": {
                "@workspace/api": {
                    "resolved": "file:../api",
                    "dependencies": {
                        "@workspace/inner": {
                            "resolved": "file:../inner",
                            "dependencies": {
                                "left-pad": {"version": "1.3.0"},
                            },
                        },
                    },
                },
            }
        }
        with patch.object(NpmEcosystem, "_npm_list", return_value=fake_npm_list):
            pkgs = eco.load_installed(deep=False)
        # left-pad lives two workspace-hops deep; we don't try to track that.
        assert pkgs == []


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
    def test_pins_direct_dependency_and_runs_install(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "app", "dependencies": {"left-pad": "^1.0.0"}})
        eco = NpmEcosystem(tmp_path)
        from chill_out.models import FixAction

        fake_install = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("chill_out.ecosystems.npm.subprocess.run", return_value=fake_install):
            log = eco.apply_fixes([FixAction(package="left-pad", version="1.2.0")])
        new_pkg = json.loads((tmp_path / "package.json").read_text())
        assert new_pkg["dependencies"]["left-pad"] == "1.2.0"
        assert "overrides" not in new_pkg
        assert any("pinned" in line for line in log)
        assert "ran: npm install" in log

    def test_pins_transitive_as_direct_dependency(self, tmp_path: Path) -> None:
        # No prior `left-pad` entry — transitive pins land in `dependencies`
        # so the resolver hoists them.
        _write_pkg_json(tmp_path / "package.json", {"name": "app", "dependencies": {"foo": "^1.0.0"}})
        eco = NpmEcosystem(tmp_path)
        from chill_out.models import FixAction

        fake_install = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("chill_out.ecosystems.npm.subprocess.run", return_value=fake_install):
            eco.apply_fixes([FixAction(package="left-pad", version="1.2.0")])
        new_pkg = json.loads((tmp_path / "package.json").read_text())
        assert new_pkg["dependencies"]["left-pad"] == "1.2.0"
        assert new_pkg["dependencies"]["foo"] == "^1.0.0"
        assert "overrides" not in new_pkg

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


class TestNpmEcosystemApplyOverrideFixes:
    def test_writes_overrides_to_package_json(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "app", "dependencies": {}})
        # Provide a lockfile so _find_lockfile resolves to tmp_path itself.
        (tmp_path / "package-lock.json").write_text("{}")
        eco = NpmEcosystem(tmp_path)
        from chill_out.models import FixAction

        fake_install = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("chill_out.ecosystems.npm.subprocess.run", return_value=fake_install):
            log = eco.apply_override_fixes([FixAction(package="left-pad", version="1.2.0")])
        assert log is not None
        new_pkg = json.loads((tmp_path / "package.json").read_text())
        assert new_pkg["overrides"]["left-pad"] == "1.2.0"
        assert any("overrode" in line for line in log)

    def test_writes_to_workspace_root_not_member(self, tmp_path: Path) -> None:
        # Workspace root has the lockfile; member is one level deep.
        _write_pkg_json(
            tmp_path / "package.json",
            {"name": "ws-root", "workspaces": ["api"], "dependencies": {}},
        )
        (tmp_path / "package-lock.json").write_text("{}")
        member = tmp_path / "api"
        member.mkdir()
        _write_pkg_json(member / "package.json", {"name": "api", "dependencies": {}})

        eco = NpmEcosystem(member)
        from chill_out.models import FixAction

        fake_install = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("chill_out.ecosystems.npm.subprocess.run", return_value=fake_install):
            eco.apply_override_fixes([FixAction(package="left-pad", version="1.2.0")])

        # Override must land in the workspace ROOT package.json, not the member's.
        root_pkg = json.loads((tmp_path / "package.json").read_text())
        member_pkg = json.loads((member / "package.json").read_text())
        assert root_pkg["overrides"]["left-pad"] == "1.2.0"
        assert "overrides" not in member_pkg

    def test_empty_actions_returns_empty_list(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "app"})
        eco = NpmEcosystem(tmp_path)
        assert eco.apply_override_fixes([]) == []


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
        eco = NpmEcosystem(tmp_path)
        with patch.object(
            NpmEcosystem,
            "_npm_list",
            return_value={"dependencies": {"x": {"version": "1.0.0"}}},
        ):
            # No declared deps can be read, so nothing is reported even though npm list returns x.
            pkgs = eco.load_installed(deep=False)
        assert pkgs == []

    def test_reports_each_distinct_version_separately(self, tmp_path: Path) -> None:
        """Multiple installed versions of the same package each get their own entry."""
        _write_pkg_json(
            tmp_path / "package.json",
            {"name": "app", "dependencies": {"big": "^1", "leaf": "1.0.0"}},
        )
        (tmp_path / "package-lock.json").write_text(json.dumps({"packages": {}}))
        # leaf appears twice: top-level at 1.0.0 (the user's pin) and nested
        # under big at 2.0.0 (a transitive that npm couldn't dedupe). Both
        # copies are real installations under their respective node_modules
        # directories and both should be reported.
        npm_list = {
            "dependencies": {
                "big": {
                    "version": "1.0.0",
                    "dependencies": {"leaf": {"version": "2.0.0"}},
                },
                "leaf": {"version": "1.0.0"},
            }
        }
        eco = NpmEcosystem(tmp_path)
        with patch.object(NpmEcosystem, "_npm_list", return_value=npm_list):
            pkgs = eco.load_installed(deep=True)
        leaf_versions = {p.version for p in pkgs if p.name == "leaf"}
        assert leaf_versions == {"1.0.0", "2.0.0"}
        # The top-level copy is a principal (declared in package.json) so
        # via_chain is empty; the nested copy is attributed to its parent.
        leaves_by_version = {p.version: p for p in pkgs if p.name == "leaf"}
        assert leaves_by_version["1.0.0"].via_chain == ()
        assert leaves_by_version["2.0.0"].via_chain == ("big",)

    def test_scopes_to_workspace_member_subtree(self, tmp_path: Path) -> None:
        """When run from a workspace member, only that member's subtree is loaded."""
        ws = tmp_path / "ws"
        member = ws / "api"
        member.mkdir(parents=True)
        _write_pkg_json(ws / "package.json", {"name": "ws", "workspaces": ["api", "web"]})
        _write_pkg_json(member / "package.json", {"name": "@org/api", "dependencies": {"a": "^1"}})
        (ws / "package-lock.json").write_text(json.dumps({"packages": {}}))
        # npm list from inside `api` walks up to the workspace root and reports
        # both members; we should only collect what's under the @org/api subtree.
        npm_list = {
            "dependencies": {
                "@org/api": {
                    "version": "1.0.0",
                    "resolved": "file:../../api",
                    "dependencies": {"a": {"version": "1.0.0"}},
                },
                "@org/web": {
                    "version": "1.0.0",
                    "resolved": "file:../../web",
                    "dependencies": {"sibling-only": {"version": "1.0.0"}},
                },
            }
        }
        eco = NpmEcosystem(member)
        with patch.object(NpmEcosystem, "_npm_list", return_value=npm_list):
            pkgs = eco.load_installed(deep=True)
        names = {p.name for p in pkgs}
        assert "a" in names
        assert "sibling-only" not in names, "sibling member's deps should be excluded"
        assert "@org/web" not in names


class TestNpmFindLockfile:
    """Lockfile lookup walks up to a workspace root when needed."""

    def test_finds_lockfile_at_root(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "app"})
        (tmp_path / "package-lock.json").write_text("{}")
        eco = NpmEcosystem(tmp_path)
        assert eco._find_lockfile() == tmp_path / "package-lock.json"

    def test_falls_back_to_node_modules_lockfile(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "app"})
        nm = tmp_path / "node_modules"
        nm.mkdir()
        (nm / ".package-lock.json").write_text("{}")
        eco = NpmEcosystem(tmp_path)
        assert eco._find_lockfile() == nm / ".package-lock.json"

    def test_walks_up_to_parent_lockfile(self, tmp_path: Path) -> None:
        # Workspace root has the lockfile; member has only its own package.json.
        (tmp_path / "package-lock.json").write_text("{}")
        member = tmp_path / "api"
        member.mkdir()
        _write_pkg_json(member / "package.json", {"name": "api"})
        eco = NpmEcosystem(member)
        assert eco._find_lockfile() == tmp_path / "package-lock.json"

    def test_walks_up_to_parent_node_modules_lockfile(self, tmp_path: Path) -> None:
        nm = tmp_path / "node_modules"
        nm.mkdir()
        (nm / ".package-lock.json").write_text("{}")
        member = tmp_path / "api"
        member.mkdir()
        _write_pkg_json(member / "package.json", {"name": "api"})
        eco = NpmEcosystem(member)
        assert eco._find_lockfile() == nm / ".package-lock.json"

    def test_returns_none_when_nothing_found(self, tmp_path: Path) -> None:
        member = tmp_path / "isolated"
        member.mkdir()
        _write_pkg_json(member / "package.json", {"name": "isolated"})
        eco = NpmEcosystem(member)
        # Without a fixture lockfile anywhere, the walk should bottom out at None.
        # The walk goes up to filesystem root, so we can't guarantee absence
        # there; instead, verify it doesn't return a path inside member or tmp_path.
        result = eco._find_lockfile()
        if result is not None:
            assert tmp_path not in result.parents and result.parent != tmp_path


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


# ---------------------------------------------------------------------------
# Workspace topology + member ownership (Tier 1)
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path: Path, members: dict[str, list[str]]) -> Path:
    """
    Lay out a minimal npm workspace under ``tmp_path``.

    ``members`` maps a member name to a list of (name, version) deps it
    declares. Each member lives under ``packages/<short>``. Returns the
    workspace root.
    """
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "ws-root",
        "workspaces": ["packages/*"],
    }))
    (tmp_path / "package-lock.json").write_text("{}")
    for member_name in members:
        short = member_name.split("/")[-1]
        member_dir = tmp_path / "packages" / short
        member_dir.mkdir(parents=True)
        (member_dir / "package.json").write_text(json.dumps({"name": member_name}))
    return tmp_path


class TestNpmWorkspaceTopology:
    def test_returns_none_when_no_workspaces_field(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({"name": "single"}))
        (tmp_path / "package-lock.json").write_text("{}")
        eco = NpmEcosystem(tmp_path)
        assert eco.workspace_topology() is None

    def test_returns_none_when_no_lockfile_and_no_pkg(self, tmp_path: Path) -> None:
        # No package.json at all
        (tmp_path / "package-lock.json").write_text("{}")
        eco = NpmEcosystem(tmp_path)
        assert eco.workspace_topology() is None

    def test_array_form_workspaces(self, tmp_path: Path) -> None:
        _make_workspace(tmp_path, {"api": [], "backend": [], "ui": []})
        eco = NpmEcosystem(tmp_path)
        topo = eco.workspace_topology()
        assert topo is not None
        assert topo.root == tmp_path
        assert set(topo.members) == {"api", "backend", "ui"}

    def test_object_form_workspaces(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({
            "name": "ws-root",
            "workspaces": {"packages": ["packages/*"]},
        }))
        (tmp_path / "package-lock.json").write_text("{}")
        for name in ("api", "backend"):
            d = tmp_path / "packages" / name
            d.mkdir(parents=True)
            (d / "package.json").write_text(json.dumps({"name": name}))
        eco = NpmEcosystem(tmp_path)
        topo = eco.workspace_topology()
        assert topo is not None
        assert set(topo.members) == {"api", "backend"}

    def test_skips_unnamed_members(self, tmp_path: Path) -> None:
        _make_workspace(tmp_path, {"api": []})
        # Add an unnamed member
        d = tmp_path / "packages" / "ghost"
        d.mkdir()
        (d / "package.json").write_text("{}")
        eco = NpmEcosystem(tmp_path)
        topo = eco.workspace_topology()
        assert topo is not None
        assert set(topo.members) == {"api"}

    def test_walks_up_from_member_to_workspace_root(self, tmp_path: Path) -> None:
        _make_workspace(tmp_path, {"api": []})
        # Run from inside the member directory
        eco = NpmEcosystem(tmp_path / "packages" / "api")
        topo = eco.workspace_topology()
        assert topo is not None
        assert topo.root == tmp_path


class TestNpmComputeMemberOwnership:
    def test_attributes_shared_dep_to_each_member(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        eco = NpmEcosystem(tmp_path)
        # Simulate npm list --all --json output where two members share lodash@4.0.0
        root_data = {
            "dependencies": {
                "api": {
                    "resolved": "file:packages/api",
                    "dependencies": {
                        "lodash": {"version": "4.0.0"},
                        "axios": {"version": "1.0.0"},
                    },
                },
                "backend": {
                    "resolved": "file:packages/backend",
                    "dependencies": {
                        "lodash": {"version": "4.0.0"},
                    },
                },
                "react": {  # registry-resolved, not a member
                    "resolved": "https://registry.npmjs.org/react/-/react-18.0.0.tgz",
                    "version": "18.0.0",
                },
            }
        }
        ownership = eco._compute_member_ownership(root_data)
        assert ownership[("lodash", "4.0.0")] == {"api", "backend"}
        assert ownership[("axios", "1.0.0")] == {"api"}
        # react is not under a file: member; should not appear
        assert ("react", "18.0.0") not in ownership

    def test_returns_empty_for_non_workspace_tree(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        eco = NpmEcosystem(tmp_path)
        root_data = {
            "dependencies": {
                "react": {
                    "resolved": "https://registry.npmjs.org/react/-/react-18.0.0.tgz",
                    "version": "18.0.0",
                },
            }
        }
        assert eco._compute_member_ownership(root_data) == {}


class TestNpmSupportsOverrides:
    def test_returns_true(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        eco = NpmEcosystem(tmp_path)
        assert eco.supports_overrides() is True

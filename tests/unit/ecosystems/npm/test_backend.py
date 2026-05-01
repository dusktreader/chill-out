"""Unit tests for NpmEcosystem and its npm backend helpers."""

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx
from chill_out.ecosystems.constants import NPM_REGISTRY
from chill_out.ecosystems.npm.backend import NpmEcosystem
from chill_out.exceptions import EcosystemError, RegistryError


def _write_pkg_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


def _make_workspace(tmp_path: Path, members: dict[str, list[str]]) -> Path:
    """
    Lay out a minimal npm workspace under `tmp_path`.

    `members` maps a member name to a list of (name, version) deps it
    declares. Each member lives under `packages/<short>`. Returns the
    workspace root.
    """
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "ws-root",
                "workspaces": ["packages/*"],
            }
        )
    )
    (tmp_path / "package-lock.json").write_text("{}")
    for member_name in members:
        short = member_name.split("/")[-1]
        member_dir = tmp_path / "packages" / short
        member_dir.mkdir(parents=True)
        (member_dir / "package.json").write_text(json.dumps({"name": member_name}))
    return tmp_path


class TestNpmEcosystemNpmList:
    def test_raises_on_unexpected_exit(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "x"})
        eco = NpmEcosystem(tmp_path)
        fake_result = type("R", (), {"returncode": 99, "stdout": "", "stderr": "kaboom"})()
        with patch("chill_out.ecosystems.npm.backend.subprocess.run", return_value=fake_result):
            with pytest.raises(EcosystemError, match="kaboom"):
                eco._npm_list(depth=1)

    def test_accepts_exit_code_1(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "x"})
        eco = NpmEcosystem(tmp_path)
        fake_result = type("R", (), {"returncode": 1, "stdout": '{"dependencies": {}}', "stderr": ""})()
        with patch("chill_out.ecosystems.npm.backend.subprocess.run", return_value=fake_result):
            assert eco._npm_list(depth=1) == {"dependencies": {}}


class TestNpmEcosystemApplyFixes:
    def test_pins_direct_dependency_and_runs_install(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "app", "dependencies": {"left-pad": "^1.0.0"}})
        eco = NpmEcosystem(tmp_path)
        from chill_out.models import FixAction

        fake_install = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("chill_out.ecosystems.npm.backend.subprocess.run", return_value=fake_install):
            result = eco.apply_fixes([FixAction(package="left-pad", version="1.2.0")])
        new_pkg = json.loads((tmp_path / "package.json").read_text())
        assert new_pkg["dependencies"]["left-pad"] == "1.2.0"
        assert "overrides" not in new_pkg
        assert any("pinned" in line for line in result.log)
        assert "ran: npm install" in result.log

    def test_pins_transitive_as_direct_dependency(self, tmp_path: Path) -> None:
        # No prior `left-pad` entry — transitive pins land in `dependencies`
        # so the resolver hoists them.
        _write_pkg_json(tmp_path / "package.json", {"name": "app", "dependencies": {"foo": "^1.0.0"}})
        eco = NpmEcosystem(tmp_path)
        from chill_out.models import FixAction

        fake_install = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("chill_out.ecosystems.npm.backend.subprocess.run", return_value=fake_install):
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
        with patch("chill_out.ecosystems.npm.backend.subprocess.run", return_value=fake_install):
            with pytest.raises(EcosystemError, match="ENOSOLO"):
                eco.apply_fixes([FixAction(package="x", version="1.0.0")])

    def test_empty_actions_noop(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "app"})
        eco = NpmEcosystem(tmp_path)
        result = eco.apply_fixes([])
        assert result.entries == []
        assert result.log == []


class TestNpmEcosystemApplyOverrideFixes:
    def test_writes_overrides_to_package_json(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "app", "dependencies": {}})
        # Provide a lockfile so _find_lockfile resolves to tmp_path itself.
        (tmp_path / "package-lock.json").write_text("{}")
        eco = NpmEcosystem(tmp_path)
        from chill_out.models import FixAction

        fake_install = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("chill_out.ecosystems.npm.backend.subprocess.run", return_value=fake_install):
            result = eco.apply_override_fixes([FixAction(package="left-pad", version="1.2.0")])
        assert result is not None
        new_pkg = json.loads((tmp_path / "package.json").read_text())
        assert new_pkg["overrides"]["left-pad"] == "1.2.0"
        assert any("overrode" in line for line in result.log)

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
        with patch("chill_out.ecosystems.npm.backend.subprocess.run", return_value=fake_install):
            eco.apply_override_fixes([FixAction(package="left-pad", version="1.2.0")])

        # Override must land in the workspace ROOT package.json, not the member's.
        root_pkg = json.loads((tmp_path / "package.json").read_text())
        member_pkg = json.loads((member / "package.json").read_text())
        assert root_pkg["overrides"]["left-pad"] == "1.2.0"
        assert "overrides" not in member_pkg

    def test_empty_actions_returns_empty_result(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "app"})
        eco = NpmEcosystem(tmp_path)
        result = eco.apply_override_fixes([])
        assert result is not None
        assert result.entries == []
        assert result.log == []


class TestNpmEcosystemRemoveManagedPin:
    @staticmethod
    def _make_pin(*, package: str, mechanism, pinned_spec: str, manifest_path: Path = Path("package.json")):
        import pendulum
        from chill_out.constants import EcosystemKind, ReleaseType
        from chill_out.state import AvoidingRelease, ManagedPin

        return ManagedPin(
            package=package,
            ecosystem=EcosystemKind.NPM,
            mechanism=mechanism,
            manifest_path=manifest_path,
            pinned_spec=pinned_spec,
            applied_at=pendulum.datetime(2026, 1, 1, tz="UTC"),
            avoiding=AvoidingRelease(
                version="9.9.9",
                release_type=ReleaseType.MAJOR,
                published_at=pendulum.datetime(2025, 12, 31, tz="UTC"),
                cooldown_days=30,
            ),
        )

    def test_direct_pin_removed_when_value_matches(self, tmp_path: Path) -> None:
        from chill_out.state import PinMechanism, RemovalOutcome

        _write_pkg_json(
            tmp_path / "package.json",
            {"name": "app", "dependencies": {"left-pad": "1.2.0", "other": "^1.0.0"}},
        )
        eco = NpmEcosystem(tmp_path)
        pin = self._make_pin(package="left-pad", mechanism=PinMechanism.DIRECT, pinned_spec="1.2.0")

        outcome = eco.remove_managed_pin(pin)

        assert outcome is RemovalOutcome.REMOVED
        new_pkg = json.loads((tmp_path / "package.json").read_text())
        assert "left-pad" not in new_pkg["dependencies"]
        assert new_pkg["dependencies"]["other"] == "^1.0.0"

    def test_direct_pin_drifted_when_value_differs(self, tmp_path: Path) -> None:
        from chill_out.state import PinMechanism, RemovalOutcome

        _write_pkg_json(
            tmp_path / "package.json",
            {"name": "app", "dependencies": {"left-pad": "1.5.0"}},
        )
        eco = NpmEcosystem(tmp_path)
        pin = self._make_pin(package="left-pad", mechanism=PinMechanism.DIRECT, pinned_spec="1.2.0")

        outcome = eco.remove_managed_pin(pin)

        assert outcome is RemovalOutcome.DRIFTED
        new_pkg = json.loads((tmp_path / "package.json").read_text())
        # Manifest left untouched on drift.
        assert new_pkg["dependencies"]["left-pad"] == "1.5.0"

    def test_direct_pin_orphan_when_package_absent(self, tmp_path: Path) -> None:
        from chill_out.state import PinMechanism, RemovalOutcome

        _write_pkg_json(
            tmp_path / "package.json",
            {"name": "app", "dependencies": {"other": "^1.0.0"}},
        )
        eco = NpmEcosystem(tmp_path)
        pin = self._make_pin(package="left-pad", mechanism=PinMechanism.DIRECT, pinned_spec="1.2.0")

        assert eco.remove_managed_pin(pin) is RemovalOutcome.ORPHAN

    def test_direct_pin_found_in_dev_dependencies(self, tmp_path: Path) -> None:
        from chill_out.state import PinMechanism, RemovalOutcome

        _write_pkg_json(
            tmp_path / "package.json",
            {"name": "app", "devDependencies": {"left-pad": "1.2.0"}},
        )
        eco = NpmEcosystem(tmp_path)
        pin = self._make_pin(package="left-pad", mechanism=PinMechanism.DIRECT, pinned_spec="1.2.0")

        assert eco.remove_managed_pin(pin) is RemovalOutcome.REMOVED
        new_pkg = json.loads((tmp_path / "package.json").read_text())
        assert "left-pad" not in new_pkg["devDependencies"]

    def test_override_pin_removed_when_value_matches(self, tmp_path: Path) -> None:
        from chill_out.state import PinMechanism, RemovalOutcome

        _write_pkg_json(
            tmp_path / "package.json",
            {"name": "app", "overrides": {"left-pad": "1.2.0", "other": "2.0.0"}},
        )
        eco = NpmEcosystem(tmp_path)
        pin = self._make_pin(package="left-pad", mechanism=PinMechanism.OVERRIDE, pinned_spec="1.2.0")

        assert eco.remove_managed_pin(pin) is RemovalOutcome.REMOVED
        new_pkg = json.loads((tmp_path / "package.json").read_text())
        assert "left-pad" not in new_pkg["overrides"]
        assert new_pkg["overrides"]["other"] == "2.0.0"

    def test_override_pin_drifted_when_value_differs(self, tmp_path: Path) -> None:
        from chill_out.state import PinMechanism, RemovalOutcome

        _write_pkg_json(
            tmp_path / "package.json",
            {"name": "app", "overrides": {"left-pad": "1.7.0"}},
        )
        eco = NpmEcosystem(tmp_path)
        pin = self._make_pin(package="left-pad", mechanism=PinMechanism.OVERRIDE, pinned_spec="1.2.0")

        assert eco.remove_managed_pin(pin) is RemovalOutcome.DRIFTED

    def test_override_pin_orphan_when_no_overrides_block(self, tmp_path: Path) -> None:
        from chill_out.state import PinMechanism, RemovalOutcome

        _write_pkg_json(tmp_path / "package.json", {"name": "app"})
        eco = NpmEcosystem(tmp_path)
        pin = self._make_pin(package="left-pad", mechanism=PinMechanism.OVERRIDE, pinned_spec="1.2.0")

        assert eco.remove_managed_pin(pin) is RemovalOutcome.ORPHAN

    def test_returns_orphan_when_manifest_missing(self, tmp_path: Path) -> None:
        from chill_out.state import PinMechanism, RemovalOutcome

        eco = NpmEcosystem(tmp_path)
        pin = self._make_pin(package="left-pad", mechanism=PinMechanism.DIRECT, pinned_spec="1.2.0")

        # No package.json on disk.
        assert eco.remove_managed_pin(pin) is RemovalOutcome.ORPHAN


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
            pkgs = eco.load_installed()
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
            pkgs = eco.load_installed()
        assert {p.name for p in pkgs} == {"x"}

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
            pkgs = eco.load_installed()
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
            pkgs = eco.load_installed()
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


class TestNpmRangeSatisfies:
    """The npm range check shells out to node; patch subprocess.run to keep tests hermetic."""

    def _eco(self, tmp_path: Path) -> NpmEcosystem:
        (tmp_path / "package.json").write_text("{}")
        return NpmEcosystem(tmp_path)

    def test_returns_true_on_node_exit_zero(self, tmp_path: Path) -> None:
        eco = self._eco(tmp_path)
        with patch("chill_out.ecosystems.npm.backend.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            assert eco.range_satisfies("1.5.0", "^1.0.0") is True

    def test_returns_false_on_node_exit_one(self, tmp_path: Path) -> None:
        eco = self._eco(tmp_path)
        with patch("chill_out.ecosystems.npm.backend.subprocess.run") as run:
            run.return_value.returncode = 1
            run.return_value.stderr = ""
            assert eco.range_satisfies("2.0.0", "^1.0.0") is False

    def test_falls_back_permissive_when_node_missing(self, tmp_path: Path) -> None:
        eco = self._eco(tmp_path)
        with patch("chill_out.ecosystems.npm.backend.subprocess.run", side_effect=FileNotFoundError):
            assert eco.range_satisfies("1.5.0", "^1.0.0") is True

    def test_falls_back_permissive_on_unexpected_exit(self, tmp_path: Path) -> None:
        eco = self._eco(tmp_path)
        with patch("chill_out.ecosystems.npm.backend.subprocess.run") as run:
            run.return_value.returncode = 2
            run.return_value.stderr = "no semver module"
            assert eco.range_satisfies("1.5.0", "^1.0.0") is True


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
        (tmp_path / "package.json").write_text(
            json.dumps(
                {
                    "name": "ws-root",
                    "workspaces": {"packages": ["packages/*"]},
                }
            )
        )
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


class TestNpmListAtWorkspaceRoot:
    def test_returns_none_when_self_root_is_workspace_root(self, tmp_path: Path) -> None:
        # Lockfile lives in self.root; no extra call needed
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "package-lock.json").write_text("{}")
        eco = NpmEcosystem(tmp_path)
        assert eco._npm_list_at_workspace_root() is None

    def test_returns_none_when_no_lockfile_anywhere(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        eco = NpmEcosystem(tmp_path)
        assert eco._npm_list_at_workspace_root() is None

    def test_runs_npm_at_workspace_root_when_self_is_member(self, tmp_path: Path) -> None:
        # Set up a workspace where self.root is a member
        _make_workspace(tmp_path, {"api": []})
        eco = NpmEcosystem(tmp_path / "packages" / "api")
        with patch("chill_out.ecosystems.npm.backend.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '{"dependencies": {}}'
            run.return_value.stderr = ""
            result = eco._npm_list_at_workspace_root()
        assert result == {"dependencies": {}}
        # cwd should be the workspace root, not the member
        assert run.call_args.kwargs["cwd"] == tmp_path


class TestNpmEcosystemGroupAttribution:
    """The installed-package loader attaches semantic groups based on the package.json section."""

    def test_direct_attributes_group_per_section(self, tmp_path: Path) -> None:
        from chill_out.constants import DependencyGroup

        _write_pkg_json(
            tmp_path / "package.json",
            {
                "name": "app",
                "dependencies": {"runtime-dep": "^1.0.0"},
                "devDependencies": {"test-tool": "^2.0.0"},
                "peerDependencies": {"plugin-host": "^3.0.0"},
                "optionalDependencies": {"nice-to-have": "^4.0.0"},
            },
        )
        eco = NpmEcosystem(tmp_path)
        fake = {
            "dependencies": {
                "runtime-dep": {"version": "1.0.0"},
                "test-tool": {"version": "2.0.0"},
                "plugin-host": {"version": "3.0.0"},
                "nice-to-have": {"version": "4.0.0"},
            }
        }
        with patch.object(NpmEcosystem, "_npm_list", return_value=fake):
            pkgs = {p.name: p for p in eco.load_installed()}
        assert pkgs["runtime-dep"].groups == (DependencyGroup.MAIN,)
        assert pkgs["test-tool"].groups == (DependencyGroup.DEV,)
        assert pkgs["plugin-host"].groups == (DependencyGroup.PEER,)
        assert pkgs["nice-to-have"].groups == (DependencyGroup.OPTIONAL,)

    def test_direct_unions_groups_when_listed_in_multiple_sections(self, tmp_path: Path) -> None:
        from chill_out.constants import DependencyGroup

        _write_pkg_json(
            tmp_path / "package.json",
            {
                "name": "app",
                "dependencies": {"shared": "^1.0.0"},
                "peerDependencies": {"shared": "^1.0.0"},
            },
        )
        eco = NpmEcosystem(tmp_path)
        fake = {"dependencies": {"shared": {"version": "1.0.0"}}}
        with patch.object(NpmEcosystem, "_npm_list", return_value=fake):
            pkgs = eco.load_installed()
        # Both groups are attached, sorted by enum value (alphabetical).
        assert pkgs[0].groups == (DependencyGroup.MAIN, DependencyGroup.PEER)

    def test_deep_propagates_top_level_group_to_transitives(self, tmp_path: Path) -> None:
        from chill_out.constants import DependencyGroup

        _write_pkg_json(
            tmp_path / "package.json",
            {
                "name": "app",
                "dependencies": {"runtime-dep": "^1"},
                "devDependencies": {"test-tool": "^2"},
            },
        )
        eco = NpmEcosystem(tmp_path)
        fake = {
            "dependencies": {
                "runtime-dep": {
                    "version": "1.0.0",
                    "dependencies": {"shared-lib": {"version": "0.5.0"}},
                },
                "test-tool": {
                    "version": "2.0.0",
                    "dependencies": {"test-only-lib": {"version": "9.0.0"}},
                },
            }
        }
        with patch.object(NpmEcosystem, "_npm_list", return_value=fake):
            with patch.object(NpmEcosystem, "_npm_list_at_workspace_root", return_value=None):
                with patch.object(NpmEcosystem, "_find_workspace_member", return_value=None):
                    pkgs = {p.name: p for p in eco.load_installed()}
        assert pkgs["runtime-dep"].groups == (DependencyGroup.MAIN,)
        assert pkgs["shared-lib"].groups == (DependencyGroup.MAIN,)
        assert pkgs["test-tool"].groups == (DependencyGroup.DEV,)
        assert pkgs["test-only-lib"].groups == (DependencyGroup.DEV,)

    def test_deep_unions_groups_when_transitive_reachable_through_multiple(self, tmp_path: Path) -> None:
        from chill_out.constants import DependencyGroup

        _write_pkg_json(
            tmp_path / "package.json",
            {
                "name": "app",
                "dependencies": {"runtime-dep": "^1"},
                "devDependencies": {"test-tool": "^2"},
            },
        )
        eco = NpmEcosystem(tmp_path)
        # Both top-levels pull in `shared-lib` at the same version.
        fake = {
            "dependencies": {
                "runtime-dep": {
                    "version": "1.0.0",
                    "dependencies": {"shared-lib": {"version": "0.5.0"}},
                },
                "test-tool": {
                    "version": "2.0.0",
                    "dependencies": {"shared-lib": {"version": "0.5.0"}},
                },
            }
        }
        with patch.object(NpmEcosystem, "_npm_list", return_value=fake):
            with patch.object(NpmEcosystem, "_npm_list_at_workspace_root", return_value=None):
                with patch.object(NpmEcosystem, "_find_workspace_member", return_value=None):
                    pkgs = {p.name: p for p in eco.load_installed()}
        assert pkgs["shared-lib"].groups == (DependencyGroup.DEV, DependencyGroup.MAIN)


class TestFormatNpmSpec:
    """Direct tests for the npm spec-rendering helper."""

    def test_exact_style_writes_bare_version(self) -> None:
        from chill_out.constants import FixStyle

        assert NpmEcosystem._format_npm_spec("1.2.3", FixStyle.EXACT) == "1.2.3"

    def test_compatible_style_writes_caret(self) -> None:
        from chill_out.constants import FixStyle

        assert NpmEcosystem._format_npm_spec("1.2.3", FixStyle.COMPATIBLE) == "^1.2.3"


class TestNpmApplyFixesCompatibleStyle:
    """End-to-end `apply_fixes` checks for `FixStyle.COMPATIBLE`."""

    def test_writes_caret_into_dependencies(self, tmp_path: Path) -> None:
        from chill_out.constants import FixStyle
        from chill_out.models import FixAction

        (tmp_path / "package.json").write_text(
            json.dumps({"name": "x", "version": "0.0.0", "dependencies": {"left-pad": "^1.0.0"}})
        )
        eco = NpmEcosystem(tmp_path)
        fake = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        action = FixAction(package="left-pad", version="1.2.0", style=FixStyle.COMPATIBLE)
        with patch("chill_out.ecosystems.npm.backend.subprocess.run", return_value=fake):
            result = eco.apply_fixes([action])
        contents = json.loads((tmp_path / "package.json").read_text())
        assert contents["dependencies"]["left-pad"] == "^1.2.0"
        assert any("^1.2.0" in line for line in result.log)

    def test_override_action_stays_exact_even_with_compatible_style(self, tmp_path: Path) -> None:
        from chill_out.constants import FixStyle
        from chill_out.models import FixAction

        (tmp_path / "package.json").write_text(json.dumps({"name": "x", "version": "0.0.0", "dependencies": {}}))
        # Pretend there's a lockfile next to the manifest so the override
        # path treats this directory as the workspace root.
        (tmp_path / "package-lock.json").write_text("{}")
        eco = NpmEcosystem(tmp_path)
        fake = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        override = FixAction(
            package="bad-transitive",
            version="1.0.0",
            via_overrides=True,
            style=FixStyle.COMPATIBLE,  # ignored on override actions
        )
        with patch("chill_out.ecosystems.npm.backend.subprocess.run", return_value=fake):
            eco.apply_fixes([override])
        contents = json.loads((tmp_path / "package.json").read_text())
        # Override was written exactly, not as a caret range.
        assert contents["overrides"]["bad-transitive"] == "1.0.0"


class TestFetchPackage:
    @respx.mock
    async def test_fetch_returns_releases(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
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
        eco = NpmEcosystem(root=tmp_path)
        info = await eco.fetch_package("left-pad", http_client)
        assert info is not None
        assert set(info.releases) == {"1.0.0", "1.3.0"}
        assert info.published_at("1.3.0") is not None

    @respx.mock
    async def test_fetch_missing_returns_none(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{NPM_REGISTRY}/nope").mock(return_value=httpx.Response(404))
        assert await NpmEcosystem(root=tmp_path).fetch_package("nope", http_client) is None

    @respx.mock
    async def test_fetch_5xx_raises(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{NPM_REGISTRY}/x").mock(return_value=httpx.Response(503))
        with pytest.raises(RegistryError, match="503"):
            await NpmEcosystem(root=tmp_path).fetch_package("x", http_client)

    @respx.mock
    async def test_fetch_transport_error_raises(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{NPM_REGISTRY}/x").mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(RegistryError, match="transport error"):
            await NpmEcosystem(root=tmp_path).fetch_package("x", http_client)

    @respx.mock
    async def test_fetch_unparsable_date_raises(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        """A single unparsable timestamp fails the whole response.

        With strict schema validation, a malformed `time` entry surfaces as a
        `RegistryError` rather than being silently dropped, so genuine drift in
        the registry payload becomes visible instead of producing a quietly
        incomplete `PackageInfo`.
        """
        respx.get(f"{NPM_REGISTRY}/x").mock(
            return_value=httpx.Response(200, json={"name": "x", "time": {"1.0.0": "garbage"}})
        )
        with pytest.raises(RegistryError, match="unexpected payload shape"):
            await NpmEcosystem(root=tmp_path).fetch_package("x", http_client)


class TestFetchVersionManifest:
    @respx.mock
    async def test_returns_merged_dependencies(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{NPM_REGISTRY}/foo/2.0.0").mock(
            return_value=httpx.Response(
                200,
                json={
                    "dependencies": {"bar": "^1.0.0"},
                    "peerDependencies": {"baz": ">=2.0"},
                },
            )
        )
        manifest = await NpmEcosystem(root=tmp_path).fetch_version_manifest("foo", "2.0.0", http_client)
        assert manifest is not None
        assert manifest.deps == {"bar": "^1.0.0", "baz": ">=2.0"}

    @respx.mock
    async def test_404_returns_none(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{NPM_REGISTRY}/foo/2.0.0").mock(return_value=httpx.Response(404))
        assert await NpmEcosystem(root=tmp_path).fetch_version_manifest("foo", "2.0.0", http_client) is None

    @respx.mock
    async def test_500_raises(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{NPM_REGISTRY}/foo/2.0.0").mock(return_value=httpx.Response(500))
        with pytest.raises(RegistryError):
            await NpmEcosystem(root=tmp_path).fetch_version_manifest("foo", "2.0.0", http_client)

    @respx.mock
    async def test_transport_error_raises(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{NPM_REGISTRY}/foo/2.0.0").mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(RegistryError):
            await NpmEcosystem(root=tmp_path).fetch_version_manifest("foo", "2.0.0", http_client)

    @respx.mock
    async def test_non_json_raises(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{NPM_REGISTRY}/foo/2.0.0").mock(return_value=httpx.Response(200, content=b"not json"))
        with pytest.raises(RegistryError):
            await NpmEcosystem(root=tmp_path).fetch_version_manifest("foo", "2.0.0", http_client)


class TestNpmCoverageGapFillers:
    """Targeted tests for defensive branches not reached by the happy-path suites."""

    def test_load_installed_skips_dep_nodes_missing_version(self, tmp_path: Path) -> None:
        """`npm list` rows lacking `version` are skipped at every nesting depth."""
        _write_pkg_json(tmp_path / "package.json", {"name": "app", "dependencies": {"real-top": "^1"}})
        npm_list = {
            "dependencies": {
                "missing-top": {},  # Hits load_installed lines 172 and 187 (top-level skip).
                "real-top": {
                    "version": "1.0.0",
                    "dependencies": {
                        "orphan-child": {},  # Hits attribute() line 165.
                        "real-child": {"version": "2.0.0"},
                    },
                },
            }
        }
        with patch.object(NpmEcosystem, "_npm_list", return_value=npm_list):
            pkgs = NpmEcosystem(tmp_path).load_installed()
        names = {p.name for p in pkgs}
        assert names == {"real-top", "real-child"}

    def test_load_installed_handles_missing_root_package_json(self, tmp_path: Path) -> None:
        """`load_installed` treats a missing root `package.json` as no-op group attribution."""
        with patch.object(NpmEcosystem, "_npm_list", return_value={"dependencies": {}}):
            pkgs = NpmEcosystem(tmp_path).load_installed()
        assert pkgs == []

    def test_load_installed_skips_unreadable_root_package_json(self, tmp_path: Path) -> None:
        """A malformed root `package.json` is logged and produces empty group metadata."""
        (tmp_path / "package.json").write_text("{not valid json")
        with patch.object(NpmEcosystem, "_npm_list", return_value={"dependencies": {"x": {"version": "1.0.0"}}}):
            pkgs = NpmEcosystem(tmp_path).load_installed()
        assert {p.name for p in pkgs} == {"x"}

    def test_compute_member_ownership_skips_member_children_missing_version(self, tmp_path: Path) -> None:
        """`_compute_member_ownership.walk` skips file:-resolved member children with no version."""
        eco = NpmEcosystem(tmp_path)
        ownership = eco._compute_member_ownership(
            {
                "dependencies": {
                    "@me/api": {
                        "resolved": "file:./packages/api",
                        "dependencies": {
                            "shadow": {},  # Skipped: no version.
                            "real": {"version": "1.0.0"},
                        },
                    }
                }
            }
        )
        assert ("real", "1.0.0") in ownership
        assert all("shadow" != name for (name, _) in ownership)


class TestNpmWorkspaceTopologyGaps:
    """Cover every defensive return in `workspace_topology`."""

    def test_returns_none_when_root_pkg_invalid_json(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{not json")
        (tmp_path / "package-lock.json").write_text("{}")
        assert NpmEcosystem(tmp_path).workspace_topology() is None

    @pytest.mark.parametrize("workspaces_value", [[], {"packages": []}])
    def test_returns_none_when_workspaces_field_empty(self, tmp_path: Path, workspaces_value) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "ws", "workspaces": workspaces_value})
        (tmp_path / "package-lock.json").write_text("{}")
        assert NpmEcosystem(tmp_path).workspace_topology() is None

    def test_skips_glob_match_that_is_a_file_not_a_directory(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "ws", "workspaces": ["packages/*"]})
        (tmp_path / "package-lock.json").write_text("{}")
        (tmp_path / "packages").mkdir()
        (tmp_path / "packages" / "stray-file").write_text("not a dir")
        assert NpmEcosystem(tmp_path).workspace_topology() is None

    def test_skips_member_dir_without_package_json(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "ws", "workspaces": ["packages/*"]})
        (tmp_path / "package-lock.json").write_text("{}")
        (tmp_path / "packages" / "ghost").mkdir(parents=True)
        assert NpmEcosystem(tmp_path).workspace_topology() is None

    def test_skips_member_with_invalid_package_json(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "ws", "workspaces": ["packages/*"]})
        (tmp_path / "package-lock.json").write_text("{}")
        broken = tmp_path / "packages" / "broken"
        broken.mkdir(parents=True)
        (broken / "package.json").write_text("{not json")
        assert NpmEcosystem(tmp_path).workspace_topology() is None

    def test_returns_none_when_no_member_has_a_name(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "ws", "workspaces": ["packages/*"]})
        (tmp_path / "package-lock.json").write_text("{}")
        nameless = tmp_path / "packages" / "nameless"
        nameless.mkdir(parents=True)
        (nameless / "package.json").write_text("{}")
        assert NpmEcosystem(tmp_path).workspace_topology() is None


class TestNpmFindWorkspaceMemberGaps:
    """Cover every defensive return in `_find_workspace_member`."""

    def _make_layout(self, tmp_path: Path) -> Path:
        """Build a workspace root with a lockfile and an empty `member/` subdir."""
        _write_pkg_json(tmp_path / "package.json", {"name": "ws"})
        (tmp_path / "package-lock.json").write_text("{}")
        member_dir = tmp_path / "member"
        member_dir.mkdir()
        return member_dir

    def test_returns_none_when_member_lacks_package_json(self, tmp_path: Path) -> None:
        member_dir = self._make_layout(tmp_path)
        eco = NpmEcosystem(member_dir)
        assert eco._find_workspace_member({"dependencies": {}}) is None

    def test_returns_none_when_member_package_json_invalid(self, tmp_path: Path) -> None:
        member_dir = self._make_layout(tmp_path)
        (member_dir / "package.json").write_text("{not json")
        eco = NpmEcosystem(member_dir)
        assert eco._find_workspace_member({"dependencies": {}}) is None

    def test_returns_none_when_member_package_json_lacks_name(self, tmp_path: Path) -> None:
        member_dir = self._make_layout(tmp_path)
        (member_dir / "package.json").write_text("{}")
        eco = NpmEcosystem(member_dir)
        assert eco._find_workspace_member({"dependencies": {}}) is None

    def test_returns_none_when_workspace_tree_lacks_member_entry(self, tmp_path: Path) -> None:
        member_dir = self._make_layout(tmp_path)
        _write_pkg_json(member_dir / "package.json", {"name": "@me/missing"})
        eco = NpmEcosystem(member_dir)
        assert eco._find_workspace_member({"dependencies": {"@me/other": {}}}) is None


class TestNpmListGaps:
    """Cover the JSON-decode and empty-stdout branches of `_npm_list`."""

    def test_returns_empty_dict_when_stdout_blank(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "x"})
        eco = NpmEcosystem(tmp_path)
        fake = type("R", (), {"returncode": 0, "stdout": "   \n", "stderr": ""})()
        with patch("chill_out.ecosystems.npm.backend.subprocess.run", return_value=fake):
            assert eco._npm_list(depth=None) == {}

    def test_raises_on_non_json_stdout(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "x"})
        eco = NpmEcosystem(tmp_path)
        fake = type("R", (), {"returncode": 0, "stdout": "not json{", "stderr": ""})()
        with patch("chill_out.ecosystems.npm.backend.subprocess.run", return_value=fake):
            with pytest.raises(EcosystemError, match="non-JSON"):
                eco._npm_list(depth=None)


class TestNpmListAtWorkspaceRootGaps:
    """Cover the bad-exit-code and JSON-decode fallbacks of `_npm_list_at_workspace_root`."""

    def test_returns_none_when_subprocess_fails(self, tmp_path: Path) -> None:
        _make_workspace(tmp_path, {"api": []})
        member = tmp_path / "packages" / "api"
        eco = NpmEcosystem(member)
        fake = type("R", (), {"returncode": 99, "stdout": "{}", "stderr": "boom"})()
        with patch("chill_out.ecosystems.npm.backend.subprocess.run", return_value=fake):
            assert eco._npm_list_at_workspace_root() is None

    def test_returns_none_when_stdout_blank(self, tmp_path: Path) -> None:
        _make_workspace(tmp_path, {"api": []})
        member = tmp_path / "packages" / "api"
        eco = NpmEcosystem(member)
        fake = type("R", (), {"returncode": 0, "stdout": "   ", "stderr": ""})()
        with patch("chill_out.ecosystems.npm.backend.subprocess.run", return_value=fake):
            assert eco._npm_list_at_workspace_root() is None

    def test_returns_none_when_stdout_non_json(self, tmp_path: Path) -> None:
        _make_workspace(tmp_path, {"api": []})
        member = tmp_path / "packages" / "api"
        eco = NpmEcosystem(member)
        fake = type("R", (), {"returncode": 0, "stdout": "not json{", "stderr": ""})()
        with patch("chill_out.ecosystems.npm.backend.subprocess.run", return_value=fake):
            assert eco._npm_list_at_workspace_root() is None


class TestNpmRegenerateLockfile:
    """Cover both branches of `regenerate_lockfile`."""

    def test_success_returns_log_line(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "x"})
        eco = NpmEcosystem(tmp_path)
        fake = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("chill_out.ecosystems.npm.backend.subprocess.run", return_value=fake):
            assert eco.regenerate_lockfile() == "ran: npm install"

    def test_raises_on_install_failure(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "x"})
        eco = NpmEcosystem(tmp_path)
        fake = type("R", (), {"returncode": 1, "stdout": "", "stderr": "broke"})()
        with patch("chill_out.ecosystems.npm.backend.subprocess.run", return_value=fake):
            with pytest.raises(EcosystemError, match="broke"):
                eco.regenerate_lockfile()


class TestNpmApplyOverrideFixesGaps:
    """Cover the no-workspace-pkg and install-failure branches of `apply_override_fixes`."""

    def test_returns_none_when_no_root_package_json(self, tmp_path: Path) -> None:
        from chill_out.models import FixAction

        eco = NpmEcosystem(tmp_path)
        result = eco.apply_override_fixes([FixAction(package="left-pad", version="1.2.0")])
        assert result is None

    def test_raises_when_install_fails(self, tmp_path: Path) -> None:
        from chill_out.models import FixAction

        _write_pkg_json(tmp_path / "package.json", {"name": "ws"})
        eco = NpmEcosystem(tmp_path)
        fake = type("R", (), {"returncode": 1, "stdout": "", "stderr": "install died"})()
        with patch("chill_out.ecosystems.npm.backend.subprocess.run", return_value=fake):
            with pytest.raises(EcosystemError, match="install died"):
                eco.apply_override_fixes([FixAction(package="left-pad", version="1.2.0")])


class TestNpmApplyFixesOverrideFallback:
    """Cover the `override_result is None` defensive branch in `apply_fixes`."""

    def test_falls_back_to_direct_pins_when_override_path_unavailable(self, tmp_path: Path) -> None:
        """
        Layout: lockfile lives at `tmp_path/package-lock.json` with no sibling `package.json`,
        and `self.root` is a child dir that does have a `package.json`. `apply_override_fixes`
        returns `None` (no workspace-root pkg), so the override action should land via direct pins.
        """
        from chill_out.models import FixAction

        (tmp_path / "package-lock.json").write_text("{}")
        member = tmp_path / "member"
        member.mkdir()
        _write_pkg_json(member / "package.json", {"name": "m"})
        eco = NpmEcosystem(member)
        action = FixAction(package="left-pad", version="1.2.0", via_overrides=True)
        fake = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("chill_out.ecosystems.npm.backend.subprocess.run", return_value=fake):
            result = eco.apply_fixes([action])
        assert any(entry.action.package == "left-pad" and not entry.via_overrides for entry in result.entries)

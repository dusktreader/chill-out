"""Unit tests for PypiEcosystem and its pypi backend helpers."""

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx
from chill_out.constants import DependencyGroup, EcosystemKind, FixStyle
from chill_out.ecosystems.constants import PYPI_REGISTRY
from chill_out.ecosystems.pypi.backend import PypiEcosystem
from chill_out.exceptions import EcosystemError, RegistryError
from chill_out.models import AppliedFix, AppliedFixes, FixAction, InstalledPackage


def _make_uv_workspace(tmp_path: Path, members: list[str]) -> Path:
    """Lay out a minimal uv workspace; return the workspace root."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'ws-root'\nversion = '0.1'\n\n[tool.uv.workspace]\nmembers = ['packages/*']\n"
    )
    for name in members:
        d = tmp_path / "packages" / name
        d.mkdir(parents=True)
        (d / "pyproject.toml").write_text(f"[project]\nname = '{name}'\nversion = '0.1'\n")
    return tmp_path


class TestPypiEcosystemLoadInstalled:
    def test_loads_all_with_via_chains(self, pypi_project: Path) -> None:
        eco = PypiEcosystem(pypi_project)
        pkgs = eco.load_installed()
        by_name = {p.name: p for p in pkgs}
        assert "requests" in by_name and by_name["requests"].via is None
        # urllib3 is in lock as a transitive entry; with no link it gets empty via chain
        assert "urllib3" in by_name

    def test_raises_without_lock(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion="0"\ndependencies=["a==1"]\n')
        eco = PypiEcosystem(tmp_path)
        with pytest.raises(EcosystemError, match="uv.lock"):
            eco.load_installed()


class TestPypiEcosystemApplyFixes:
    def test_pins_existing_dependency(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion="0"\ndependencies = ["requests>=2.0"]\n')
        eco = PypiEcosystem(tmp_path)
        fake = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("chill_out.ecosystems.pypi.backend.subprocess.run", return_value=fake):
            result = eco.apply_fixes([FixAction(package="requests", version="2.30.0")])
        contents = (tmp_path / "pyproject.toml").read_text()
        assert "requests==2.30.0" in contents
        assert any("pinned requests" in line for line in result.log)
        assert "ran: uv lock" in result.log

    def test_adds_when_missing(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion="0"\ndependencies = []\n')
        eco = PypiEcosystem(tmp_path)
        fake = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("chill_out.ecosystems.pypi.backend.subprocess.run", return_value=fake):
            result = eco.apply_fixes([FixAction(package="newpkg", version="1.2.3")])
        contents = (tmp_path / "pyproject.toml").read_text()
        assert "newpkg==1.2.3" in contents
        assert any("added newpkg" in line for line in result.log)

    def test_uv_lock_failure_raises(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion="0"\ndependencies = ["a==1"]\n')
        eco = PypiEcosystem(tmp_path)
        fake = type("R", (), {"returncode": 1, "stdout": "", "stderr": "lock failed"})()
        with patch("chill_out.ecosystems.pypi.backend.subprocess.run", return_value=fake):
            with pytest.raises(EcosystemError, match="lock failed"):
                eco.apply_fixes([FixAction(package="a", version="0.9.0")])

    def test_empty_actions_noop(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion="0"\n')
        eco = PypiEcosystem(tmp_path)
        result = eco.apply_fixes([])
        assert result.entries == []
        assert result.log == []


class TestNormalize:
    def test_lowercases_and_collapses_separators(self) -> None:
        assert PypiEcosystem._normalize("Foo_Bar.baz--qux") == "foo-bar-baz-qux"


class TestAbsorbRequirements:
    """Unit tests for folding `pyproject.toml` requirement arrays into the spec map.

    `_absorb_requirements` mutates a caller-owned `specs` dict in place. The
    tests construct a fresh dict per case, call the helper with various
    inputs, and assert on the resulting state.
    """

    def test_none_items_is_noop(self) -> None:
        """A missing section (`None`) leaves the accumulator untouched."""
        specs: dict[str, tuple[str, set[DependencyGroup]]] = {}
        PypiEcosystem._absorb_requirements(specs, None, DependencyGroup.MAIN)
        assert specs == {}

    def test_empty_items_is_noop(self) -> None:
        """An empty array (declared but empty section) also leaves it untouched."""
        specs: dict[str, tuple[str, set[DependencyGroup]]] = {}
        PypiEcosystem._absorb_requirements(specs, [], DependencyGroup.MAIN)
        assert specs == {}

    def test_inserts_new_requirement(self) -> None:
        """A first-seen package lands as `(raw_spec, {group})`."""
        specs: dict[str, tuple[str, set[DependencyGroup]]] = {}
        PypiEcosystem._absorb_requirements(specs, ["requests>=2.0"], DependencyGroup.MAIN)
        assert specs == {"requests": ("requests>=2.0", {DependencyGroup.MAIN})}

    def test_normalizes_package_name_for_key(self) -> None:
        """Keys use PEP 503 normalization; raw spec preserves the original casing."""
        specs: dict[str, tuple[str, set[DependencyGroup]]] = {}
        PypiEcosystem._absorb_requirements(specs, ["Foo_Bar>=1"], DependencyGroup.MAIN)
        assert "foo-bar" in specs
        assert specs["foo-bar"][0] == "Foo_Bar>=1"

    def test_second_occurrence_unions_group(self) -> None:
        """A repeated package gets its new group unioned in; raw_spec is preserved."""
        specs: dict[str, tuple[str, set[DependencyGroup]]] = {}
        PypiEcosystem._absorb_requirements(specs, ["requests>=2.0"], DependencyGroup.MAIN)
        PypiEcosystem._absorb_requirements(specs, ["requests>=2.0"], DependencyGroup.DEV)
        assert specs == {"requests": ("requests>=2.0", {DependencyGroup.MAIN, DependencyGroup.DEV})}

    def test_first_raw_spec_wins_on_repeat(self) -> None:
        """When a package appears twice with different specs, the first one is kept."""
        specs: dict[str, tuple[str, set[DependencyGroup]]] = {}
        PypiEcosystem._absorb_requirements(specs, ["requests>=2.0"], DependencyGroup.MAIN)
        PypiEcosystem._absorb_requirements(specs, ["requests>=2.5"], DependencyGroup.DEV)
        # The raw_spec field stays as the first-seen value.
        assert specs["requests"][0] == "requests>=2.0"
        # Both groups accumulate.
        assert specs["requests"][1] == {DependencyGroup.MAIN, DependencyGroup.DEV}

    def test_unparsable_requirement_logged_and_skipped(self) -> None:
        """A malformed entry is skipped silently (with a warning), not raised."""
        specs: dict[str, tuple[str, set[DependencyGroup]]] = {}
        # `not a requirement!` doesn't parse as PEP 508; it should be dropped,
        # and the well-formed entry that follows should still be absorbed.
        PypiEcosystem._absorb_requirements(specs, ["not a requirement!", "requests>=2.0"], DependencyGroup.MAIN)
        assert specs == {"requests": ("requests>=2.0", {DependencyGroup.MAIN})}

    def test_mutation_in_place_does_not_replace_dict(self) -> None:
        """The caller's dict identity is preserved; only its contents change."""
        specs: dict[str, tuple[str, set[DependencyGroup]]] = {}
        original_id = id(specs)
        PypiEcosystem._absorb_requirements(specs, ["foo>=1"], DependencyGroup.MAIN)
        assert id(specs) == original_id
        assert specs == {"foo": ("foo>=1", {DependencyGroup.MAIN})}

    def test_multiple_packages_in_one_call(self) -> None:
        """A single array with several entries absorbs them all."""
        specs: dict[str, tuple[str, set[DependencyGroup]]] = {}
        PypiEcosystem._absorb_requirements(specs, ["foo>=1", "bar==2", "baz"], DependencyGroup.OPTIONAL)
        assert set(specs.keys()) == {"foo", "bar", "baz"}
        for name in ("foo", "bar", "baz"):
            assert specs[name][1] == {DependencyGroup.OPTIONAL}


class TestLocateWorkspaceRoot:
    """Unit tests for the upward walk that finds a `[tool.uv.workspace]` declaration."""

    def test_returns_none_when_no_pyproject_anywhere(self, tmp_path: Path) -> None:
        """Walking from a directory with no pyproject.toml above it yields `(None, None)`."""
        start = tmp_path / "deep" / "nested"
        start.mkdir(parents=True)
        # tmp_path itself has no pyproject.toml; the walk runs out at the filesystem root.
        assert PypiEcosystem._locate_workspace_root(start) == (None, None)

    def test_returns_none_when_pyproject_has_no_workspace_table(self, tmp_path: Path) -> None:
        """A reachable pyproject.toml without `[tool.uv.workspace]` doesn't count."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'app'\n")
        assert PypiEcosystem._locate_workspace_root(tmp_path) == (None, None)

    def test_finds_workspace_at_starting_directory(self, tmp_path: Path) -> None:
        """A `[tool.uv.workspace]` in the start directory is returned immediately."""
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'root'\n[tool.uv.workspace]\nmembers = ['packages/*']\n"
        )
        (workspace_root, workspace_table) = PypiEcosystem._locate_workspace_root(tmp_path)
        assert workspace_root == tmp_path.resolve()
        assert workspace_table is not None
        assert list(workspace_table["members"]) == ["packages/*"]

    def test_walks_upward_to_find_workspace(self, tmp_path: Path) -> None:
        """When `start` is a child directory, the walk climbs to the parent that declares it."""
        (tmp_path / "pyproject.toml").write_text("[tool.uv.workspace]\nmembers = ['*']\n")
        nested = tmp_path / "packages" / "foo"
        nested.mkdir(parents=True)
        (nested / "pyproject.toml").write_text("[project]\nname = 'foo'\n")
        (workspace_root, _) = PypiEcosystem._locate_workspace_root(nested)
        assert workspace_root == tmp_path.resolve()

    def test_first_match_wins_during_walk(self, tmp_path: Path) -> None:
        """The walk stops at the nearest ancestor with a workspace declaration."""
        # Outer workspace at tmp_path.
        (tmp_path / "pyproject.toml").write_text("[tool.uv.workspace]\nmembers = ['outer/*']\n")
        # Inner workspace at tmp_path/inner, closer to `start`.
        inner = tmp_path / "inner"
        inner.mkdir()
        (inner / "pyproject.toml").write_text("[tool.uv.workspace]\nmembers = ['inner/*']\n")
        nested = inner / "packages" / "foo"
        nested.mkdir(parents=True)
        (workspace_root, workspace_table) = PypiEcosystem._locate_workspace_root(nested)
        # The inner workspace wins because it's encountered first during the upward walk.
        assert workspace_root == inner.resolve()
        assert workspace_table is not None
        assert list(workspace_table["members"]) == ["inner/*"]

    def test_malformed_pyproject_raises(self, tmp_path: Path) -> None:
        """A `pyproject.toml` that doesn't parse as TOML surfaces as `EcosystemError`."""
        (tmp_path / "pyproject.toml").write_text("not = valid = toml")
        with pytest.raises(EcosystemError, match="Malformed pyproject.toml"):
            PypiEcosystem._locate_workspace_root(tmp_path)

    def test_non_table_workspace_value_raises(self, tmp_path: Path) -> None:
        """A `[tool.uv.workspace]` value that isn't a table raises rather than silently passing."""
        (tmp_path / "pyproject.toml").write_text("[tool.uv]\nworkspace = 'not-a-table'\n")
        with pytest.raises(EcosystemError, match=r"Malformed \[tool\.uv\.workspace\]"):
            PypiEcosystem._locate_workspace_root(tmp_path)

    def test_intermediate_pyproject_without_workspace_continues_walk(self, tmp_path: Path) -> None:
        """A non-workspace `pyproject.toml` in between doesn't stop the search."""
        (tmp_path / "pyproject.toml").write_text("[tool.uv.workspace]\nmembers = ['*']\n")
        intermediate = tmp_path / "middle"
        intermediate.mkdir()
        (intermediate / "pyproject.toml").write_text("[project]\nname = 'middle'\n")
        leaf = intermediate / "leaf"
        leaf.mkdir()
        (workspace_root, _) = PypiEcosystem._locate_workspace_root(leaf)
        # The walk skips `intermediate` (no workspace table) and lands on tmp_path.
        assert workspace_root == tmp_path.resolve()


class TestResolveWorkspaceExcludes:
    """Unit tests for expanding `[tool.uv.workspace].exclude` glob patterns."""

    def test_empty_patterns_returns_empty_set(self) -> None:
        """No exclude patterns means nothing is excluded."""
        result = PypiEcosystem._resolve_workspace_excludes(Path("/nonexistent"), [])
        assert result == set()

    def test_pattern_matching_no_paths_returns_empty_set(self, tmp_path: Path) -> None:
        """A pattern with no matches contributes nothing."""
        result = PypiEcosystem._resolve_workspace_excludes(tmp_path, ["does-not-exist-*"])
        assert result == set()

    def test_glob_pattern_resolves_matched_paths(self, tmp_path: Path) -> None:
        """Wildcard expansion produces resolved (canonical) paths in the result."""
        (tmp_path / "skip-a").mkdir()
        (tmp_path / "skip-b").mkdir()
        (tmp_path / "keep").mkdir()
        result = PypiEcosystem._resolve_workspace_excludes(tmp_path, ["skip-*"])
        assert result == {(tmp_path / "skip-a").resolve(), (tmp_path / "skip-b").resolve()}

    def test_multiple_patterns_union(self, tmp_path: Path) -> None:
        """Patterns accumulate; matches from any of them appear in the result."""
        (tmp_path / "alpha").mkdir()
        (tmp_path / "beta").mkdir()
        result = PypiEcosystem._resolve_workspace_excludes(tmp_path, ["alpha", "beta"])
        assert result == {(tmp_path / "alpha").resolve(), (tmp_path / "beta").resolve()}

    def test_paths_are_resolved(self, tmp_path: Path) -> None:
        """Returned paths use `Path.resolve()` so equality checks survive symlinks."""
        target = tmp_path / "real"
        target.mkdir()
        link = tmp_path / "via-link"
        link.symlink_to(target)
        result = PypiEcosystem._resolve_workspace_excludes(tmp_path, ["via-link"])
        # The link expands to the resolved target, not to the symlink path itself.
        assert result == {target.resolve()}


class TestDiscoverWorkspaceMembers:
    """Unit tests for resolving `[tool.uv.workspace].members` patterns to a member map."""

    @staticmethod
    def _write_member(path: Path, project_name: str | None) -> None:
        """Materialize a candidate member directory with an optional `project.name`."""
        path.mkdir(parents=True, exist_ok=True)
        if project_name is None:
            (path / "pyproject.toml").write_text("[project]\nversion = '0.1'\n")
        else:
            (path / "pyproject.toml").write_text(f"[project]\nname = '{project_name}'\n")

    def test_empty_patterns_returns_empty_dict(self, tmp_path: Path) -> None:
        """No member patterns means no members."""
        result = PypiEcosystem._discover_workspace_members(tmp_path, [], set())
        assert result == {}

    def test_basic_member_discovered_by_glob(self, tmp_path: Path) -> None:
        """A directory matched by a glob with a valid `pyproject.toml` is included."""
        self._write_member(tmp_path / "packages" / "foo", "foo-pkg")
        result = PypiEcosystem._discover_workspace_members(tmp_path, ["packages/*"], set())
        assert result == {"foo-pkg": tmp_path / "packages" / "foo"}

    def test_files_are_skipped(self, tmp_path: Path) -> None:
        """Non-directory matches (e.g. files) are filtered out."""
        (tmp_path / "packages").mkdir()
        (tmp_path / "packages" / "not-a-dir").write_text("hello")
        result = PypiEcosystem._discover_workspace_members(tmp_path, ["packages/*"], set())
        assert result == {}

    def test_excluded_member_skipped(self, tmp_path: Path) -> None:
        """Directories whose resolved path is in `excluded` are filtered out."""
        self._write_member(tmp_path / "packages" / "foo", "foo")
        self._write_member(tmp_path / "packages" / "bar", "bar")
        excluded = {(tmp_path / "packages" / "foo").resolve()}
        result = PypiEcosystem._discover_workspace_members(tmp_path, ["packages/*"], excluded)
        assert set(result.keys()) == {"bar"}

    def test_member_without_pyproject_skipped(self, tmp_path: Path) -> None:
        """Candidate directories without a `pyproject.toml` are not real members."""
        (tmp_path / "packages" / "empty").mkdir(parents=True)
        result = PypiEcosystem._discover_workspace_members(tmp_path, ["packages/*"], set())
        assert result == {}

    def test_member_without_project_name_skipped(self, tmp_path: Path) -> None:
        """`pyproject.toml` without `project.name` can't be keyed; skip it."""
        self._write_member(tmp_path / "packages" / "nameless", project_name=None)
        result = PypiEcosystem._discover_workspace_members(tmp_path, ["packages/*"], set())
        assert result == {}

    def test_member_name_is_normalized(self, tmp_path: Path) -> None:
        """Keys in the result use PEP 503 normalization."""
        self._write_member(tmp_path / "packages" / "Foo_Bar", "Foo_Bar.Baz")
        result = PypiEcosystem._discover_workspace_members(tmp_path, ["packages/*"], set())
        assert "foo-bar-baz" in result

    def test_malformed_pyproject_raises(self, tmp_path: Path) -> None:
        """A `pyproject.toml` that doesn't parse as TOML surfaces as `EcosystemError`."""
        (tmp_path / "packages" / "broken").mkdir(parents=True)
        (tmp_path / "packages" / "broken" / "pyproject.toml").write_text("not = valid = toml")
        with pytest.raises(EcosystemError, match="Malformed member pyproject.toml"):
            PypiEcosystem._discover_workspace_members(tmp_path, ["packages/*"], set())

    def test_multiple_patterns_collect_all_members(self, tmp_path: Path) -> None:
        """Several patterns accumulate; each match contributes one entry."""
        self._write_member(tmp_path / "apps" / "app-a", "app-a")
        self._write_member(tmp_path / "libs" / "lib-a", "lib-a")
        result = PypiEcosystem._discover_workspace_members(tmp_path, ["apps/*", "libs/*"], set())
        assert set(result.keys()) == {"app-a", "lib-a"}


class TestFindViaChain:
    """Unit tests for the reverse-BFS that attributes transitives to principals.

    The function takes a precomputed reverse graph (`required_by`) and the set
    of principal package names, and returns the shortest chain of intermediate
    packages between the queried name and the nearest principal.
    """

    def test_direct_dependency_returns_principal_only(self) -> None:
        """A package one hop from a principal returns just the principal."""
        required_by = {"foo": {"app"}}
        assert PypiEcosystem._find_via_chain("foo", required_by, principals={"app"}) == ("app",)

    def test_two_hop_chain_returns_full_path(self) -> None:
        """Intermediate parents come first, the principal last."""
        # Edges: app -> foo -> bar.
        required_by = {"foo": {"app"}, "bar": {"foo"}}
        assert PypiEcosystem._find_via_chain("bar", required_by, principals={"app"}) == ("foo", "app")

    def test_three_hop_chain_returns_full_path(self) -> None:
        """Chain order is parent-first, principal-last regardless of depth."""
        # Edges: app -> a -> b -> c.
        required_by = {"a": {"app"}, "b": {"a"}, "c": {"b"}}
        assert PypiEcosystem._find_via_chain("c", required_by, principals={"app"}) == ("b", "a", "app")

    def test_orphan_returns_empty_tuple(self) -> None:
        """A package with no entry in `required_by` is unreachable from any principal."""
        required_by = {"foo": {"app"}}
        assert PypiEcosystem._find_via_chain("orphan", required_by, principals={"app"}) == ()

    def test_disconnected_component_returns_empty_tuple(self) -> None:
        """A reachable parent that itself does not reach a principal yields no path."""
        # Edges: x -> y, but y is not a principal and has no parents.
        required_by = {"x": {"y"}}
        assert PypiEcosystem._find_via_chain("x", required_by, principals={"app"}) == ()

    def test_multiple_principals_picks_nearest(self) -> None:
        """BFS guarantees the shortest path wins when several principals are reachable."""
        # Edges: app1 -> foo -> bar, app2 -> bar (direct).
        # Both principals can reach `bar`, but app2 is one hop, app1 is two.
        required_by = {"foo": {"app1"}, "bar": {"foo", "app2"}}
        assert PypiEcosystem._find_via_chain("bar", required_by, principals={"app1", "app2"}) == ("app2",)

    def test_principal_queried_directly_returns_empty_tuple(self) -> None:
        """Calling with a principal name short-circuits to no chain.

        Documented behavior: callers should guard with `if name not in principals`,
        but the function tolerates the call and returns `()` because the BFS
        skips the starting node when checking for a principal hit.
        """
        required_by = {"foo": {"app"}}
        assert PypiEcosystem._find_via_chain("app", required_by, principals={"app"}) == ()

    def test_cycle_does_not_loop_forever(self) -> None:
        """Cycles in the reverse graph are bounded by the visited set.

        `uv.lock` should not produce cycles in practice, but the BFS should
        terminate even if one slips through.
        """
        # Edges: a -> b -> a (cycle), neither is a principal.
        required_by = {"a": {"b"}, "b": {"a"}}
        assert PypiEcosystem._find_via_chain("a", required_by, principals={"app"}) == ()


class TestPropagateGroups:
    """Unit tests for forward-walking dependency-group labels onto transitives.

    Each principal seeds a BFS that unions its label set into every reachable
    package. Multiple principals' label sets accumulate by set-union on shared
    descendants.
    """

    def test_principal_gets_its_own_groups(self) -> None:
        """A principal with no transitives still appears in the result."""
        direct_specs = {"app": ("app>=1", {DependencyGroup.MAIN})}
        deps_by_name: dict[str, set[str]] = {"app": set()}
        result = PypiEcosystem._propagate_groups(direct_specs, deps_by_name)
        assert result == {"app": {DependencyGroup.MAIN}}

    def test_transitive_inherits_principal_groups(self) -> None:
        """Reachable transitives carry the principal's label set."""
        # Edges: app -> foo -> bar.
        direct_specs = {"app": ("app>=1", {DependencyGroup.MAIN})}
        deps_by_name = {"app": {"foo"}, "foo": {"bar"}, "bar": set()}
        result = PypiEcosystem._propagate_groups(direct_specs, deps_by_name)
        assert result == {
            "app": {DependencyGroup.MAIN},
            "foo": {DependencyGroup.MAIN},
            "bar": {DependencyGroup.MAIN},
        }

    def test_shared_descendant_unions_groups(self) -> None:
        """A package reachable from two principals collects both label sets."""
        # Edges: main-app -> shared, dev-app -> shared.
        direct_specs = {
            "main-app": ("main-app>=1", {DependencyGroup.MAIN}),
            "dev-app": ("dev-app>=1", {DependencyGroup.DEV}),
        }
        deps_by_name = {"main-app": {"shared"}, "dev-app": {"shared"}, "shared": set()}
        result = PypiEcosystem._propagate_groups(direct_specs, deps_by_name)
        assert result["shared"] == {DependencyGroup.MAIN, DependencyGroup.DEV}

    def test_unreachable_package_is_absent(self) -> None:
        """Packages with no path from any principal don't appear in the result."""
        # `orphan` exists in deps_by_name but no principal reaches it.
        direct_specs = {"app": ("app>=1", {DependencyGroup.MAIN})}
        deps_by_name = {"app": {"foo"}, "foo": set(), "orphan": set()}
        result = PypiEcosystem._propagate_groups(direct_specs, deps_by_name)
        assert "orphan" not in result

    def test_principal_carries_multiple_groups(self) -> None:
        """A principal declared in several sections propagates all its labels."""
        direct_specs = {"app": ("app>=1", {DependencyGroup.MAIN, DependencyGroup.OPTIONAL})}
        deps_by_name = {"app": {"foo"}, "foo": set()}
        result = PypiEcosystem._propagate_groups(direct_specs, deps_by_name)
        assert result["foo"] == {DependencyGroup.MAIN, DependencyGroup.OPTIONAL}

    def test_cycle_does_not_loop_forever(self) -> None:
        """Cycles in the forward graph are bounded by the per-principal visited set."""
        # Edges: app -> a -> b -> a (cycle).
        direct_specs = {"app": ("app>=1", {DependencyGroup.MAIN})}
        deps_by_name = {"app": {"a"}, "a": {"b"}, "b": {"a"}}
        result = PypiEcosystem._propagate_groups(direct_specs, deps_by_name)
        assert result == {
            "app": {DependencyGroup.MAIN},
            "a": {DependencyGroup.MAIN},
            "b": {DependencyGroup.MAIN},
        }

    def test_empty_inputs_return_empty_dict(self) -> None:
        """No principals means no propagation."""
        assert PypiEcosystem._propagate_groups({}, {}) == {}

    def test_missing_deps_entry_treated_as_no_children(self) -> None:
        """A principal absent from `deps_by_name` is still labeled, but has no descendants."""
        direct_specs = {"app": ("app>=1", {DependencyGroup.MAIN})}
        deps_by_name: dict[str, set[str]] = {}
        result = PypiEcosystem._propagate_groups(direct_specs, deps_by_name)
        assert result == {"app": {DependencyGroup.MAIN}}


class TestParseLockPackages:
    """Unit tests for the single-pass lockfile-shape parser.

    Inputs use plain dicts because tomlkit's parsed tables expose the same
    `.get()` interface; chill-out only relies on duck-typed access here.
    """

    def test_empty_input_returns_three_empty_dicts(self) -> None:
        """No packages in, no graph out."""
        version_by_name, deps_by_name, required_by = PypiEcosystem._parse_lock_packages([])
        assert version_by_name == {}
        assert deps_by_name == {}
        assert required_by == {}

    def test_single_package_no_dependencies(self) -> None:
        """A leaf package shows up in versions and forward graph, not in reverse."""
        packages = [{"name": "foo", "version": "1.0", "dependencies": []}]
        version_by_name, deps_by_name, required_by = PypiEcosystem._parse_lock_packages(packages)
        assert version_by_name == {"foo": "1.0"}
        assert deps_by_name == {"foo": set()}
        assert required_by == {}

    def test_normalizes_names_in_all_three_dicts(self) -> None:
        """PEP 503 normalization is applied to package names and dep names alike."""
        packages = [
            {"name": "Foo_Bar", "version": "1.0", "dependencies": [{"name": "Baz.Qux"}]},
            {"name": "Baz.Qux", "version": "2.0", "dependencies": []},
        ]
        version_by_name, deps_by_name, required_by = PypiEcosystem._parse_lock_packages(packages)
        assert version_by_name == {"foo-bar": "1.0", "baz-qux": "2.0"}
        assert deps_by_name == {"foo-bar": {"baz-qux"}, "baz-qux": set()}
        assert required_by == {"baz-qux": {"foo-bar"}}

    def test_builds_forward_and_reverse_graphs_in_one_pass(self) -> None:
        """Edges land in both `deps_by_name` and `required_by` symmetrically."""
        # Edges: app -> foo, app -> bar, foo -> bar.
        packages = [
            {
                "name": "app",
                "version": "0.1",
                "dependencies": [{"name": "foo"}, {"name": "bar"}],
            },
            {"name": "foo", "version": "1.0", "dependencies": [{"name": "bar"}]},
            {"name": "bar", "version": "2.0", "dependencies": []},
        ]
        version_by_name, deps_by_name, required_by = PypiEcosystem._parse_lock_packages(packages)
        assert version_by_name == {"app": "0.1", "foo": "1.0", "bar": "2.0"}
        assert deps_by_name == {"app": {"foo", "bar"}, "foo": {"bar"}, "bar": set()}
        assert required_by == {"foo": {"app"}, "bar": {"app", "foo"}}

    def test_missing_dependencies_key_treated_as_no_deps(self) -> None:
        """Lockfile entries can omit `dependencies` entirely for leaf packages."""
        packages = [{"name": "foo", "version": "1.0"}]
        _, deps_by_name, required_by = PypiEcosystem._parse_lock_packages(packages)
        assert deps_by_name == {"foo": set()}
        assert required_by == {}

    def test_nameless_package_entry_silently_skipped(self) -> None:
        """Entries without a `name` field are not real packages and get skipped."""
        packages = [
            {"version": "0.0"},  # no name; skipped
            {"name": "foo", "version": "1.0", "dependencies": []},
        ]
        version_by_name, _, _ = PypiEcosystem._parse_lock_packages(packages)
        assert version_by_name == {"foo": "1.0"}

    def test_missing_version_raises(self) -> None:
        """A package entry without a version is a corrupted lockfile."""
        packages = [{"name": "foo", "dependencies": []}]
        with pytest.raises(EcosystemError, match="uv.lock entry for 'foo' is missing a version"):
            PypiEcosystem._parse_lock_packages(packages)

    def test_empty_version_raises(self) -> None:
        """An empty-string version is treated the same as a missing one."""
        packages = [{"name": "foo", "version": "", "dependencies": []}]
        with pytest.raises(EcosystemError, match="uv.lock entry for 'foo' is missing a version"):
            PypiEcosystem._parse_lock_packages(packages)

    def test_dependency_entry_without_name_raises(self) -> None:
        """A dep entry without a `name` field can't be resolved to a graph node."""
        packages = [
            {"name": "app", "version": "0.1", "dependencies": [{"version": "1.0"}]},
        ]
        with pytest.raises(EcosystemError, match="uv.lock dependency entry under 'app' is missing a name"):
            PypiEcosystem._parse_lock_packages(packages)

    def test_multiple_parents_accumulate_in_required_by(self) -> None:
        """A shared dep collects every package that requires it."""
        packages = [
            {"name": "a", "version": "0.1", "dependencies": [{"name": "shared"}]},
            {"name": "b", "version": "0.1", "dependencies": [{"name": "shared"}]},
            {"name": "shared", "version": "1.0", "dependencies": []},
        ]
        _, _, required_by = PypiEcosystem._parse_lock_packages(packages)
        assert required_by == {"shared": {"a", "b"}}


class TestBuildInstalledPackages:
    """Unit tests for the assembly step that joins the parsed graph dicts.

    Each test exercises a small synthetic graph and asserts on the resulting
    `InstalledPackage` records: ecosystem identity, attribution chain, and
    the deterministic sort of group labels.
    """

    def test_principal_has_empty_via_chain(self) -> None:
        """A principal package has no attribution chain by definition."""
        result = PypiEcosystem._build_installed_packages(
            ecosystem=EcosystemKind.PYPI,
            version_by_name={"app": "1.0"},
            required_by={},
            groups_by_pkg={"app": {DependencyGroup.MAIN}},
            principals={"app"},
        )
        assert len(result) == 1
        assert result[0] == InstalledPackage(
            name="app",
            version="1.0",
            ecosystem=EcosystemKind.PYPI,
            via_chain=(),
            groups=(DependencyGroup.MAIN,),
        )

    def test_transitive_gets_via_chain_back_to_principal(self) -> None:
        """A transitive carries the reverse-BFS chain from `_find_via_chain`."""
        # Edges: app -> foo (so required_by["foo"] == {"app"}).
        result = PypiEcosystem._build_installed_packages(
            ecosystem=EcosystemKind.PYPI,
            version_by_name={"app": "1.0", "foo": "2.0"},
            required_by={"foo": {"app"}},
            groups_by_pkg={"app": {DependencyGroup.MAIN}, "foo": {DependencyGroup.MAIN}},
            principals={"app"},
        )
        by_name = {pkg.name: pkg for pkg in result}
        assert by_name["app"].via_chain == ()
        assert by_name["foo"].via_chain == ("app",)

    def test_groups_sorted_alphabetically_by_value(self) -> None:
        """Group tuples are sorted by `group.value` for deterministic output."""
        # MAIN, DEV, OPTIONAL all on one package; result must be stable.
        result = PypiEcosystem._build_installed_packages(
            ecosystem=EcosystemKind.PYPI,
            version_by_name={"app": "1.0"},
            required_by={},
            groups_by_pkg={"app": {DependencyGroup.OPTIONAL, DependencyGroup.MAIN, DependencyGroup.DEV}},
            principals={"app"},
        )
        groups = result[0].groups
        # Sort key is `group.value`; assert the resulting tuple matches that order.
        expected = tuple(
            sorted({DependencyGroup.OPTIONAL, DependencyGroup.MAIN, DependencyGroup.DEV}, key=lambda g: g.value)
        )
        assert groups == expected

    def test_missing_groups_entry_yields_empty_tuple(self) -> None:
        """Packages absent from `groups_by_pkg` get `groups=()`."""
        result = PypiEcosystem._build_installed_packages(
            ecosystem=EcosystemKind.PYPI,
            version_by_name={"orphan": "0.1"},
            required_by={},
            groups_by_pkg={},
            principals=set(),
        )
        assert result[0].groups == ()

    def test_ecosystem_kind_is_propagated(self) -> None:
        """The `ecosystem` argument lands on every produced record."""
        result = PypiEcosystem._build_installed_packages(
            ecosystem=EcosystemKind.NPM,
            version_by_name={"foo": "1.0"},
            required_by={},
            groups_by_pkg={},
            principals=set(),
        )
        assert result[0].ecosystem == EcosystemKind.NPM

    def test_empty_version_map_returns_empty_list(self) -> None:
        """No packages in, empty list out."""
        result = PypiEcosystem._build_installed_packages(
            ecosystem=EcosystemKind.PYPI,
            version_by_name={},
            required_by={},
            groups_by_pkg={},
            principals=set(),
        )
        assert result == []

    def test_unreachable_transitive_gets_empty_via_chain(self) -> None:
        """A non-principal with no parents in the reverse graph has no chain."""
        # `orphan` is not a principal and not in `required_by`, so BFS finds nothing.
        result = PypiEcosystem._build_installed_packages(
            ecosystem=EcosystemKind.PYPI,
            version_by_name={"app": "1.0", "orphan": "0.1"},
            required_by={},
            groups_by_pkg={"app": {DependencyGroup.MAIN}},
            principals={"app"},
        )
        by_name = {pkg.name: pkg for pkg in result}
        assert by_name["orphan"].via_chain == ()
        assert by_name["orphan"].groups == ()


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


class TestPypiWorkspaceTopology:
    def test_returns_none_when_no_workspace_section(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'solo'\nversion = '0.1'\n")
        eco = PypiEcosystem(tmp_path)
        assert eco.workspace_topology() is None

    def test_returns_none_when_no_pyproject(self, tmp_path: Path) -> None:
        eco = PypiEcosystem(tmp_path)
        assert eco.workspace_topology() is None

    def test_discovers_members(self, tmp_path: Path) -> None:
        _make_uv_workspace(tmp_path, ["api", "backend"])
        eco = PypiEcosystem(tmp_path)
        topo = eco.workspace_topology()
        assert topo is not None
        assert topo.root == tmp_path.resolve()
        assert set(topo.members) == {"api", "backend"}

    def test_normalizes_member_names(self, tmp_path: Path) -> None:
        # PEP 503: My_Package -> my-package
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'ws'\nversion = '0.1'\n\n[tool.uv.workspace]\nmembers = ['packages/*']\n"
        )
        d = tmp_path / "packages" / "weird"
        d.mkdir(parents=True)
        (d / "pyproject.toml").write_text("[project]\nname = 'My_Package'\nversion = '0.1'\n")
        eco = PypiEcosystem(tmp_path)
        topo = eco.workspace_topology()
        assert topo is not None
        assert "my-package" in topo.members

    def test_excludes_listed_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'ws'\nversion = '0.1'\n"
            "\n[tool.uv.workspace]\nmembers = ['packages/*']\nexclude = ['packages/legacy']\n"
        )
        for name in ("api", "legacy"):
            d = tmp_path / "packages" / name
            d.mkdir(parents=True)
            (d / "pyproject.toml").write_text(f"[project]\nname = '{name}'\nversion = '0.1'\n")
        eco = PypiEcosystem(tmp_path)
        topo = eco.workspace_topology()
        assert topo is not None
        assert set(topo.members) == {"api"}

    def test_walks_up_from_member(self, tmp_path: Path) -> None:
        _make_uv_workspace(tmp_path, ["api"])
        eco = PypiEcosystem(tmp_path / "packages" / "api")
        topo = eco.workspace_topology()
        assert topo is not None
        assert topo.root == tmp_path.resolve()


class TestPypiSupportsOverrides:
    def test_returns_true(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0.1'\n")
        eco = PypiEcosystem(tmp_path)
        assert eco.supports_overrides() is True


class TestPypiApplyOverrideFixes:
    def test_writes_override_dependencies_at_workspace_root(self, tmp_path: Path) -> None:
        _make_uv_workspace(tmp_path, ["api"])
        member = tmp_path / "packages" / "api"
        eco = PypiEcosystem(member)
        action = FixAction(
            package="urllib3",
            version="2.0.7",
            via_overrides=True,
        )
        with patch("chill_out.ecosystems.pypi.backend.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            result = eco.apply_override_fixes([action])
        assert result is not None
        # Override should have been written to the workspace root, not the member
        root_doc = (tmp_path / "pyproject.toml").read_text()
        assert "override-dependencies" in root_doc
        assert "urllib3==2.0.7" in root_doc
        # Member's pyproject is untouched
        member_doc = (member / "pyproject.toml").read_text()
        assert "override-dependencies" not in member_doc
        # Subprocess ran in the workspace root
        run.assert_called_once()
        assert run.call_args.kwargs["cwd"] == tmp_path.resolve()

    def test_dedupes_existing_override_for_same_package(self, tmp_path: Path) -> None:
        # Workspace root already has an override for urllib3
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'ws'\nversion = '0.1'\n"
            "\n[tool.uv.workspace]\nmembers = ['packages/*']\n"
            "\n[tool.uv]\noverride-dependencies = ['urllib3==1.0.0', 'requests==2.0.0']\n"
        )
        d = tmp_path / "packages" / "api"
        d.mkdir(parents=True)
        (d / "pyproject.toml").write_text("[project]\nname = 'api'\nversion = '0.1'\n")
        eco = PypiEcosystem(d)
        action = FixAction(
            package="urllib3",
            version="2.0.7",
            via_overrides=True,
        )
        with patch("chill_out.ecosystems.pypi.backend.subprocess.run") as run:
            run.return_value.returncode = 0
            result = eco.apply_override_fixes([action])
        assert result is not None
        text = (tmp_path / "pyproject.toml").read_text()
        # Old urllib3 entry should be gone, new one present, requests preserved
        assert "urllib3==1.0.0" not in text
        assert "urllib3==2.0.7" in text
        assert "requests==2.0.0" in text

    def test_returns_none_when_no_pyproject_at_root(self, tmp_path: Path) -> None:
        # No pyproject.toml exists, so no workspace, and self.root has none either
        eco = PypiEcosystem(tmp_path)
        action = FixAction(
            package="x",
            version="1.0.0",
            via_overrides=True,
        )
        assert eco.apply_override_fixes([action]) is None

    def test_returns_empty_result_for_empty_actions(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0.1'\n")
        eco = PypiEcosystem(tmp_path)
        result = eco.apply_override_fixes([])
        assert result is not None
        assert result.entries == []
        assert result.log == []

    def test_raises_when_uv_lock_fails(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0.1'\n")
        eco = PypiEcosystem(tmp_path)
        action = FixAction(
            package="x",
            version="1.0.0",
            via_overrides=True,
        )
        with patch("chill_out.ecosystems.pypi.backend.subprocess.run") as run:
            run.return_value.returncode = 1
            run.return_value.stderr = "resolution failed"
            with pytest.raises(EcosystemError, match="resolution failed"):
                eco.apply_override_fixes([action])


class TestPypiApplyFixesRouting:
    def test_routes_via_overrides_actions_to_apply_override_fixes(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'x'\nversion = '0.1'\ndependencies = ['plain==1.0.0']\n"
        )
        eco = PypiEcosystem(tmp_path)
        direct = FixAction(
            package="plain",
            version="1.5.0",
            via_overrides=False,
        )
        override = FixAction(
            package="urllib3",
            version="2.0.7",
            via_overrides=True,
        )
        with (
            patch("chill_out.ecosystems.pypi.backend.subprocess.run") as run,
            patch.object(
                PypiEcosystem,
                "apply_override_fixes",
                return_value=AppliedFixes(
                    entries=[
                        AppliedFix(
                            action=override,
                            pinned_spec="urllib3==2.0.7",
                            via_overrides=True,
                            manifest_path=Path("pyproject.toml"),
                        )
                    ],
                    log=["overrode urllib3==2.0.7 (workspace root)"],
                ),
            ) as override_mock,
        ):
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            result = eco.apply_fixes([direct, override])
        # Both code paths got their respective actions
        override_mock.assert_called_once()
        assert override_mock.call_args.args[0] == [override]
        assert any("plain" in line for line in result.log)
        assert any("urllib3" in line for line in result.log)


class TestPypiEcosystemRemoveManagedPin:
    @staticmethod
    def _make_pin(*, package: str, mechanism, pinned_spec: str, manifest_path: Path = Path("pyproject.toml")):
        import pendulum
        from chill_out.constants import EcosystemKind, ReleaseType
        from chill_out.state import AvoidingRelease, ManagedPin

        return ManagedPin(
            package=package,
            ecosystem=EcosystemKind.PYPI,
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

    def test_direct_pin_removed_from_project_dependencies(self, tmp_path: Path) -> None:
        from chill_out.state import PinMechanism, RemovalOutcome

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="x"\nversion="0"\ndependencies = ["requests==2.32.3", "click>=8.0"]\n'
        )
        eco = PypiEcosystem(tmp_path)
        pin = self._make_pin(package="requests", mechanism=PinMechanism.DIRECT, pinned_spec="requests==2.32.3")

        assert eco.remove_managed_pin(pin) is RemovalOutcome.REMOVED
        text = (tmp_path / "pyproject.toml").read_text()
        assert "requests" not in text
        assert "click>=8.0" in text

    def test_direct_pin_drifted_when_value_differs(self, tmp_path: Path) -> None:
        from chill_out.state import PinMechanism, RemovalOutcome

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="x"\nversion="0"\ndependencies = ["requests==2.40.0"]\n'
        )
        eco = PypiEcosystem(tmp_path)
        pin = self._make_pin(package="requests", mechanism=PinMechanism.DIRECT, pinned_spec="requests==2.32.3")

        assert eco.remove_managed_pin(pin) is RemovalOutcome.DRIFTED
        text = (tmp_path / "pyproject.toml").read_text()
        assert "requests==2.40.0" in text  # untouched

    def test_direct_pin_orphan_when_package_absent(self, tmp_path: Path) -> None:
        from chill_out.state import PinMechanism, RemovalOutcome

        (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion="0"\ndependencies = ["click>=8.0"]\n')
        eco = PypiEcosystem(tmp_path)
        pin = self._make_pin(package="requests", mechanism=PinMechanism.DIRECT, pinned_spec="requests==2.32.3")

        assert eco.remove_managed_pin(pin) is RemovalOutcome.ORPHAN

    def test_direct_pin_found_in_dependency_groups(self, tmp_path: Path) -> None:
        from chill_out.state import PinMechanism, RemovalOutcome

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="x"\nversion="0"\ndependencies = []\n[dependency-groups]\ndev = ["pytest==8.0.0"]\n'
        )
        eco = PypiEcosystem(tmp_path)
        pin = self._make_pin(package="pytest", mechanism=PinMechanism.DIRECT, pinned_spec="pytest==8.0.0")

        assert eco.remove_managed_pin(pin) is RemovalOutcome.REMOVED
        text = (tmp_path / "pyproject.toml").read_text()
        assert "pytest" not in text

    def test_override_pin_removed_from_uv_overrides(self, tmp_path: Path) -> None:
        from chill_out.state import PinMechanism, RemovalOutcome

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="x"\nversion="0"\ndependencies = []\n'
            "[tool.uv]\n"
            'override-dependencies = ["requests==2.32.3", "click==8.1.0"]\n'
        )
        eco = PypiEcosystem(tmp_path)
        pin = self._make_pin(package="requests", mechanism=PinMechanism.OVERRIDE, pinned_spec="requests==2.32.3")

        assert eco.remove_managed_pin(pin) is RemovalOutcome.REMOVED
        text = (tmp_path / "pyproject.toml").read_text()
        assert "requests" not in text
        assert "click==8.1.0" in text

    def test_override_pin_drifted_when_value_differs(self, tmp_path: Path) -> None:
        from chill_out.state import PinMechanism, RemovalOutcome

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="x"\nversion="0"\ndependencies = []\n'
            "[tool.uv]\n"
            'override-dependencies = ["requests==2.40.0"]\n'
        )
        eco = PypiEcosystem(tmp_path)
        pin = self._make_pin(package="requests", mechanism=PinMechanism.OVERRIDE, pinned_spec="requests==2.32.3")

        assert eco.remove_managed_pin(pin) is RemovalOutcome.DRIFTED

    def test_override_pin_orphan_when_no_overrides_section(self, tmp_path: Path) -> None:
        from chill_out.state import PinMechanism, RemovalOutcome

        (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion="0"\ndependencies = []\n')
        eco = PypiEcosystem(tmp_path)
        pin = self._make_pin(package="requests", mechanism=PinMechanism.OVERRIDE, pinned_spec="requests==2.32.3")

        assert eco.remove_managed_pin(pin) is RemovalOutcome.ORPHAN

    def test_returns_orphan_when_manifest_missing(self, tmp_path: Path) -> None:
        from chill_out.state import PinMechanism, RemovalOutcome

        eco = PypiEcosystem(tmp_path)
        pin = self._make_pin(package="requests", mechanism=PinMechanism.DIRECT, pinned_spec="requests==2.32.3")

        assert eco.remove_managed_pin(pin) is RemovalOutcome.ORPHAN


class TestPypiEcosystemGroupAttribution:
    """The installed-package loader attaches semantic groups based on the pyproject.toml section."""

    def test_direct_attributes_main_dev_optional(self, tmp_path: Path) -> None:
        from chill_out.constants import DependencyGroup

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="x"\nversion="0"\n'
            'dependencies = ["main-dep==1.0"]\n'
            "[project.optional-dependencies]\n"
            'dev = ["dev-extra==2.0"]\n'
            'aws = ["aws-extra==3.0"]\n'
            "[dependency-groups]\n"
            'dev = ["dev-tool==4.0"]\n'
            'docs = ["docs-tool==5.0"]\n'
        )
        (tmp_path / "uv.lock").write_text(
            "version = 1\n\n"
            '[[package]]\nname = "main-dep"\nversion = "1.0"\n\n'
            '[[package]]\nname = "dev-extra"\nversion = "2.0"\n\n'
            '[[package]]\nname = "aws-extra"\nversion = "3.0"\n\n'
            '[[package]]\nname = "dev-tool"\nversion = "4.0"\n\n'
            '[[package]]\nname = "docs-tool"\nversion = "5.0"\n'
        )
        eco = PypiEcosystem(tmp_path)
        pkgs = {p.name: p for p in eco.load_installed()}
        assert pkgs["main-dep"].groups == (DependencyGroup.MAIN,)
        # The literal extras name "dev" maps to DEV; everything else is OPTIONAL.
        assert pkgs["dev-extra"].groups == (DependencyGroup.DEV,)
        assert pkgs["aws-extra"].groups == (DependencyGroup.OPTIONAL,)
        # The literal dependency-groups name "dev" maps to DEV; others map to OPTIONAL.
        assert pkgs["dev-tool"].groups == (DependencyGroup.DEV,)
        assert pkgs["docs-tool"].groups == (DependencyGroup.OPTIONAL,)

    def test_direct_unions_groups_when_listed_in_multiple_sections(self, tmp_path: Path) -> None:
        from chill_out.constants import DependencyGroup

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="x"\nversion="0"\ndependencies = ["shared==1"]\n[dependency-groups]\ndev = ["shared==1"]\n'
        )
        (tmp_path / "uv.lock").write_text('version = 1\n\n[[package]]\nname = "shared"\nversion = "1"\n')
        eco = PypiEcosystem(tmp_path)
        pkgs = {p.name: p for p in eco.load_installed()}
        assert pkgs["shared"].groups == (DependencyGroup.DEV, DependencyGroup.MAIN)

    def test_propagates_groups_to_transitives(self, tmp_path: Path) -> None:
        from chill_out.constants import DependencyGroup

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="root"\nversion="0"\n'
            'dependencies = ["main-dep==1.0"]\n'
            "[dependency-groups]\n"
            'dev = ["dev-tool==2.0"]\n'
        )
        # Hand-crafted uv.lock with two principals that each pull in their own
        # transitive plus a shared one.
        (tmp_path / "uv.lock").write_text(
            "version = 1\n\n"
            '[[package]]\nname = "main-dep"\nversion = "1.0"\n'
            "dependencies = [\n"
            '  { name = "shared-lib" },\n'
            "]\n\n"
            '[[package]]\nname = "dev-tool"\nversion = "2.0"\n'
            "dependencies = [\n"
            '  { name = "shared-lib" },\n'
            '  { name = "dev-only-lib" },\n'
            "]\n\n"
            '[[package]]\nname = "shared-lib"\nversion = "0.5"\n\n'
            '[[package]]\nname = "dev-only-lib"\nversion = "9.0"\n'
        )
        eco = PypiEcosystem(tmp_path)
        pkgs = {p.name: p for p in eco.load_installed()}
        assert pkgs["main-dep"].groups == (DependencyGroup.MAIN,)
        assert pkgs["dev-tool"].groups == (DependencyGroup.DEV,)
        assert pkgs["dev-only-lib"].groups == (DependencyGroup.DEV,)
        # Shared transitive accumulates both groups (sorted alphabetically by enum value).
        assert pkgs["shared-lib"].groups == (DependencyGroup.DEV, DependencyGroup.MAIN)


class TestFormatPypiSpec:
    """Direct tests for the spec-rendering helper used by both fix paths."""

    def test_exact_style_writes_double_equals(self) -> None:
        from packaging.specifiers import SpecifierSet

        out = PypiEcosystem._format_pypi_spec("requests", "2.30.0", SpecifierSet(">=2.0"), FixStyle.EXACT)
        assert out == "requests==2.30.0"

    def test_compatible_style_caps_at_next_major(self) -> None:
        out = PypiEcosystem._format_pypi_spec("requests", "2.30.0", None, FixStyle.COMPATIBLE)
        # No prior lower bound -> safe version becomes the floor.
        assert out == "requests>=2.30.0,<3.0.0"

    def test_compatible_style_preserves_existing_lower_bound(self) -> None:
        from packaging.specifiers import SpecifierSet

        out = PypiEcosystem._format_pypi_spec("rich", "14.3.4", SpecifierSet(">=14.0"), FixStyle.COMPATIBLE)
        assert out == "rich>=14.0,<15.0.0"

    def test_compatible_style_picks_highest_usable_lower_bound(self) -> None:
        from packaging.specifiers import SpecifierSet

        # Multiple >= clauses (uncommon but legal); pick the largest that
        # doesn't exceed the safe version.
        spec = SpecifierSet(">=14.0,>=14.1,>=14.5")
        out = PypiEcosystem._format_pypi_spec("rich", "14.3.4", spec, FixStyle.COMPATIBLE)
        # 14.5 > 14.3.4, so it's discarded; 14.1 is the highest usable.
        assert out == "rich>=14.1,<15.0.0"

    def test_compatible_style_discards_lower_bound_above_safe(self) -> None:
        from packaging.specifiers import SpecifierSet

        spec = SpecifierSet(">=15.0")
        out = PypiEcosystem._format_pypi_spec("rich", "14.3.4", spec, FixStyle.COMPATIBLE)
        # The user's floor would forbid the safe version itself; fall back
        # to the safe version as the floor.
        assert out == "rich>=14.3.4,<15.0.0"

    def test_compatible_style_handles_zero_major(self) -> None:
        # 0.x releases get capped at <1.0.0, matching the next-major rule
        # uniformly. (Some ecosystems treat 0.x as "every minor breaks",
        # but the user opted into compatible explicitly.)
        out = PypiEcosystem._format_pypi_spec("alpha", "0.7.2", None, FixStyle.COMPATIBLE)
        assert out == "alpha>=0.7.2,<1.0.0"

    def test_compatible_style_falls_back_to_exact_for_invalid_version(self) -> None:
        # Non-PEP-440 strings can't be parsed for major; fall back to exact.
        out = PypiEcosystem._format_pypi_spec("weird", "not-a-version", None, FixStyle.COMPATIBLE)
        assert out == "weird==not-a-version"


class TestPypiApplyFixesCompatibleStyle:
    """End-to-end `apply_fixes` checks for `FixStyle.COMPATIBLE`."""

    def test_writes_range_preserving_lower_bound(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion="0"\ndependencies = ["rich>=14.0"]\n')
        eco = PypiEcosystem(tmp_path)
        fake = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        action = FixAction(package="rich", version="14.3.4", style=FixStyle.COMPATIBLE)
        with patch("chill_out.ecosystems.pypi.backend.subprocess.run", return_value=fake):
            result = eco.apply_fixes([action])
        contents = (tmp_path / "pyproject.toml").read_text()
        assert "rich>=14.0,<15.0.0" in contents
        assert "rich==14.3.4" not in contents
        assert any("rich>=14.0,<15.0.0" in line for line in result.log)

    def test_writes_range_when_dependency_is_brand_new(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion="0"\ndependencies = []\n')
        eco = PypiEcosystem(tmp_path)
        fake = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        action = FixAction(package="newpkg", version="1.2.3", style=FixStyle.COMPATIBLE)
        with patch("chill_out.ecosystems.pypi.backend.subprocess.run", return_value=fake):
            result = eco.apply_fixes([action])
        contents = (tmp_path / "pyproject.toml").read_text()
        assert "newpkg>=1.2.3,<2.0.0" in contents
        assert any("newpkg>=1.2.3,<2.0.0" in line for line in result.log)

    def test_override_action_stays_exact_even_with_compatible_style(self, tmp_path: Path) -> None:
        # Direct entry exists for unrelated package so the direct-fix path
        # has something to do; the override goes through the override
        # writer, which always emits `==`.
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="x"\nversion="0"\ndependencies = ["unrelated>=1.0"]\n'
        )
        eco = PypiEcosystem(tmp_path)
        fake = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        override = FixAction(
            package="bad-transitive",
            version="1.0.0",
            via_overrides=True,
            style=FixStyle.COMPATIBLE,  # ignored on override actions
        )
        with patch("chill_out.ecosystems.pypi.backend.subprocess.run", return_value=fake):
            eco.apply_fixes([override])
        contents = (tmp_path / "pyproject.toml").read_text()
        # Override was written exactly, not as a range.
        assert "bad-transitive==1.0.0" in contents
        assert ">=1.0.0,<2.0.0" not in contents


class TestFetchPackage:
    @respx.mock
    async def test_fetch_returns_earliest_upload_per_version(
        self, tmp_path: Path, http_client: httpx.AsyncClient
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
                        "2.30.0": [{"upload_time_iso_8601": "2023-04-01T00:00:00.000Z"}],
                        "empty": [],
                    }
                },
            )
        )
        info = await PypiEcosystem(root=tmp_path).fetch_package("requests", http_client)
        assert info is not None
        assert "empty" not in info.releases
        published = info.published_at("2.31.0")
        assert published is not None
        assert published.to_iso8601_string().startswith("2023-05-22T15:00:00")

    @respx.mock
    async def test_404_returns_none(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{PYPI_REGISTRY}/nope/json").mock(return_value=httpx.Response(404))
        assert await PypiEcosystem(root=tmp_path).fetch_package("nope", http_client) is None

    @respx.mock
    async def test_5xx_raises(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{PYPI_REGISTRY}/x/json").mock(return_value=httpx.Response(500))
        with pytest.raises(RegistryError):
            await PypiEcosystem(root=tmp_path).fetch_package("x", http_client)


class TestPypiCoverageGapFillers:
    """Targeted tests for defensive branches that aren't reached by the happy-path suites."""

    def test_workspace_topology_returns_none_when_no_members_match(self, tmp_path: Path) -> None:
        """A workspace declaration whose globs match no member directories yields no topology."""
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'ws'\nversion = '0.1'\n\n[tool.uv.workspace]\nmembers = ['packages/*']\n"
        )
        # Create the parent dir but no member subdirs.
        (tmp_path / "packages").mkdir()
        eco = PypiEcosystem(tmp_path)
        assert eco.workspace_topology() is None

    def test_apply_fixes_falls_back_to_direct_when_override_path_unavailable(self, tmp_path: Path) -> None:
        """When `apply_override_fixes` returns None, override-tagged actions are pinned directly."""
        from chill_out.constants import FixStyle

        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.1"\ndependencies = []\n')
        eco = PypiEcosystem(tmp_path)
        action = FixAction(package="urllib3", version="2.0.7", via_overrides=True, style=FixStyle.EXACT)
        # Force apply_override_fixes to return None to exercise the fallback path.
        with patch.object(PypiEcosystem, "apply_override_fixes", return_value=None):
            with patch("chill_out.ecosystems.pypi.backend.subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stderr = ""
                result = eco.apply_fixes([action])
        # The action should land in [project.dependencies] via the direct-pin fallback.
        text = (tmp_path / "pyproject.toml").read_text()
        assert "urllib3" in text
        assert any("urllib3" in entry.pinned_spec for entry in result.entries)

    def test_apply_override_fixes_skips_unparsable_existing_overrides(self, tmp_path: Path) -> None:
        """Existing override entries that don't parse as `Requirement` are kept as-is."""
        from chill_out.constants import FixStyle

        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'x'\nversion = '0.1'\n"
            "\n[tool.uv]\noverride-dependencies = ['~~~not-a-requirement~~~', 'click==8.0.0']\n"
        )
        eco = PypiEcosystem(tmp_path)
        action = FixAction(package="urllib3", version="2.0.7", via_overrides=True, style=FixStyle.EXACT)
        with patch("chill_out.ecosystems.pypi.backend.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            result = eco.apply_override_fixes([action])
        assert result is not None
        text = (tmp_path / "pyproject.toml").read_text()
        # The unparsable entry survived (we only drop entries whose name matches an action).
        assert "~~~not-a-requirement~~~" in text
        assert "click==8.0.0" in text
        assert "urllib3==2.0.7" in text

    def test_remove_pin_returns_orphan_when_dependencies_section_absent(self, tmp_path: Path) -> None:
        """Direct removal returns ORPHAN when `[project]` exists but has no `dependencies` array."""
        from chill_out.state import PinMechanism, RemovalOutcome

        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.1"\n')
        eco = PypiEcosystem(tmp_path)
        pin = TestPypiEcosystemRemoveManagedPin._make_pin(
            package="requests", mechanism=PinMechanism.DIRECT, pinned_spec="requests==2.32.3"
        )
        assert eco.remove_managed_pin(pin) is RemovalOutcome.ORPHAN

    def test_remove_pin_skips_unparsable_entries_in_optional_dependencies(self, tmp_path: Path) -> None:
        """Garbled rows in `optional-dependencies` are skipped without raising."""
        from chill_out.state import PinMechanism, RemovalOutcome

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0.1"\ndependencies = []\n'
            "[project.optional-dependencies]\n"
            "extra = ['~~~bogus~~~', 'requests==2.32.3']\n"
        )
        eco = PypiEcosystem(tmp_path)
        pin = TestPypiEcosystemRemoveManagedPin._make_pin(
            package="requests", mechanism=PinMechanism.DIRECT, pinned_spec="requests==2.32.3"
        )
        assert eco.remove_managed_pin(pin) is RemovalOutcome.REMOVED

    def test_remove_override_pin_orphan_when_tool_section_missing(self, tmp_path: Path) -> None:
        """Override removal returns ORPHAN when `[tool]` itself is missing."""
        from chill_out.state import PinMechanism, RemovalOutcome

        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.1"\n')
        eco = PypiEcosystem(tmp_path)
        pin = TestPypiEcosystemRemoveManagedPin._make_pin(
            package="requests", mechanism=PinMechanism.OVERRIDE, pinned_spec="requests==2.32.3"
        )
        assert eco.remove_managed_pin(pin) is RemovalOutcome.ORPHAN

    def test_remove_override_pin_orphan_when_uv_section_missing(self, tmp_path: Path) -> None:
        """Override removal returns ORPHAN when `[tool.uv]` is missing under `[tool]`."""
        from chill_out.state import PinMechanism, RemovalOutcome

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0.1"\n[tool.something-else]\nfoo = "bar"\n'
        )
        eco = PypiEcosystem(tmp_path)
        pin = TestPypiEcosystemRemoveManagedPin._make_pin(
            package="requests", mechanism=PinMechanism.OVERRIDE, pinned_spec="requests==2.32.3"
        )
        assert eco.remove_managed_pin(pin) is RemovalOutcome.ORPHAN

    def test_remove_override_pin_skips_unparsable_overrides(self, tmp_path: Path) -> None:
        """Garbled rows in `[tool.uv.override-dependencies]` are skipped without raising."""
        from chill_out.state import PinMechanism, RemovalOutcome

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0.1"\ndependencies = []\n'
            "[tool.uv]\noverride-dependencies = ['~~~bogus~~~', 'requests==2.32.3']\n"
        )
        eco = PypiEcosystem(tmp_path)
        pin = TestPypiEcosystemRemoveManagedPin._make_pin(
            package="requests", mechanism=PinMechanism.OVERRIDE, pinned_spec="requests==2.32.3"
        )
        assert eco.remove_managed_pin(pin) is RemovalOutcome.REMOVED

    def test_pin_dependency_skips_unparsable_entries(self, tmp_path: Path) -> None:
        """Garbled rows in `dependencies` are skipped during pin replacement."""
        from chill_out.constants import FixStyle

        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = \"x\"\nversion = \"0.1\"\ndependencies = ['~~~bogus~~~', 'requests']\n"
        )
        eco = PypiEcosystem(tmp_path)
        action = FixAction(package="requests", version="2.32.3", via_overrides=False, style=FixStyle.EXACT)
        with patch("chill_out.ecosystems.pypi.backend.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            result = eco.apply_fixes([action])
        text = (tmp_path / "pyproject.toml").read_text()
        assert "~~~bogus~~~" in text  # still there, just skipped
        assert "requests==2.32.3" in text
        assert any("requests" in entry.pinned_spec for entry in result.entries)

    def test_pin_dependency_finds_match_in_optional_dependencies(self, tmp_path: Path) -> None:
        """Pin replacement walks `[project.optional-dependencies]` extras."""
        from chill_out.constants import FixStyle

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0.1"\ndependencies = []\n'
            "[project.optional-dependencies]\n"
            "extra = ['requests']\n"
        )
        eco = PypiEcosystem(tmp_path)
        action = FixAction(package="requests", version="2.32.3", via_overrides=False, style=FixStyle.EXACT)
        with patch("chill_out.ecosystems.pypi.backend.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            eco.apply_fixes([action])
        text = (tmp_path / "pyproject.toml").read_text()
        assert "requests==2.32.3" in text

    def test_pin_dependency_finds_match_in_dependency_groups(self, tmp_path: Path) -> None:
        """Pin replacement walks `[dependency-groups.*]` arrays."""
        from chill_out.constants import FixStyle

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0.1"\ndependencies = []\n[dependency-groups]\ndev = [\'pytest\']\n'
        )
        eco = PypiEcosystem(tmp_path)
        action = FixAction(package="pytest", version="8.0.0", via_overrides=False, style=FixStyle.EXACT)
        with patch("chill_out.ecosystems.pypi.backend.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            eco.apply_fixes([action])
        text = (tmp_path / "pyproject.toml").read_text()
        assert "pytest==8.0.0" in text

    def test_existing_lower_bound_skips_unparsable_versions(self, tmp_path: Path) -> None:
        """`SpecifierSet` validates versions at parse time so the InvalidVersion path is unreachable.

        Kept here as documentation: the defensive `except InvalidVersion: continue` in
        `_existing_lower_bound` exists to guard against any future packaging-internal
        change where `sp.version` could leak through validation. The branch is marked
        `# pragma: no cover` in the implementation since constructing a SpecifierSet
        with such a value isn't possible through the public API.
        """
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version

        spec = SpecifierSet(">=1.0.0,<2.0.0")
        # Confirm the happy path still picks the right floor.
        assert PypiEcosystem._existing_lower_bound(spec, Version("3.0.0")) == "1.0.0"


class TestFetchVersionManifest:
    @respx.mock
    async def test_404_returns_none(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{PYPI_REGISTRY}/foo/1.0/json").mock(return_value=httpx.Response(404))
        assert await PypiEcosystem(root=tmp_path).fetch_version_manifest("foo", "1.0", http_client) is None

    @respx.mock
    async def test_500_raises(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{PYPI_REGISTRY}/foo/1.0/json").mock(return_value=httpx.Response(500))
        with pytest.raises(RegistryError):
            await PypiEcosystem(root=tmp_path).fetch_version_manifest("foo", "1.0", http_client)

    @respx.mock
    async def test_transport_error_raises(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{PYPI_REGISTRY}/foo/1.0/json").mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(RegistryError):
            await PypiEcosystem(root=tmp_path).fetch_version_manifest("foo", "1.0", http_client)

    @respx.mock
    async def test_non_json_raises(self, tmp_path: Path, http_client: httpx.AsyncClient) -> None:
        respx.get(f"{PYPI_REGISTRY}/foo/1.0/json").mock(return_value=httpx.Response(200, content=b"not json"))
        with pytest.raises(RegistryError):
            await PypiEcosystem(root=tmp_path).fetch_version_manifest("foo", "1.0", http_client)


class TestPypiCoverageGapFillersExtra:
    """Force-fire the remaining defensive branches in `_remove_pypi_*_entry` and `_pin_dependency`."""

    def test_remove_override_skips_non_matching_entries_then_orphan(self, tmp_path: Path) -> None:
        """An override array of unrelated entries iterates fully and returns ORPHAN."""
        from chill_out.state import PinMechanism, RemovalOutcome

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0.1"\ndependencies = []\n'
            "[tool.uv]\noverride-dependencies = ['click==8.0.0', 'numpy==1.0.0']\n"
        )
        eco = PypiEcosystem(tmp_path)
        pin = TestPypiEcosystemRemoveManagedPin._make_pin(
            package="requests", mechanism=PinMechanism.OVERRIDE, pinned_spec="requests==2.32.3"
        )
        assert eco.remove_managed_pin(pin) is RemovalOutcome.ORPHAN

    def test_remove_override_orphan_when_uv_section_lacks_overrides_field(self, tmp_path: Path) -> None:
        """`[tool.uv]` exists but has no `override-dependencies` key at all."""
        from chill_out.state import PinMechanism, RemovalOutcome

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0.1"\ndependencies = []\n'
            "[tool.uv]\nindex-url = 'https://example.com/simple'\n"
        )
        eco = PypiEcosystem(tmp_path)
        pin = TestPypiEcosystemRemoveManagedPin._make_pin(
            package="requests", mechanism=PinMechanism.OVERRIDE, pinned_spec="requests==2.32.3"
        )
        assert eco.remove_managed_pin(pin) is RemovalOutcome.ORPHAN

    def test_remove_direct_skips_non_matching_entries_in_dependencies(self, tmp_path: Path) -> None:
        """`[project.dependencies]` packed with non-matching entries falls through to ORPHAN."""
        from chill_out.state import PinMechanism, RemovalOutcome

        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = \"x\"\nversion = \"0.1\"\ndependencies = ['click==8.0.0', 'numpy==1.0.0']\n"
        )
        eco = PypiEcosystem(tmp_path)
        pin = TestPypiEcosystemRemoveManagedPin._make_pin(
            package="requests", mechanism=PinMechanism.DIRECT, pinned_spec="requests==2.32.3"
        )
        assert eco.remove_managed_pin(pin) is RemovalOutcome.ORPHAN

    def test_load_installed_skips_extras_only_requirements(self, tmp_path: Path) -> None:
        """`requires_dist` rows whose marker mentions `extra` are filtered out of the dep graph."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\ndependencies = ["alpha"]\n')
        (tmp_path / "uv.lock").write_text(
            "version = 1\n"
            "\n[[package]]\nname = 'app'\nversion = '0.1'\n"
            "[[package.dependencies]]\nname = 'alpha'\n"
            "\n[[package]]\nname = 'alpha'\nversion = '1.0.0'\n"
            "[[package.dependencies]]\nname = 'beta'\n"
            "[package.metadata]\n"
            "requires-dist = [\n"
            '    "beta",\n'
            "    \"gamma; extra == 'docs'\",\n"
            "]\n"
            "\n[[package]]\nname = 'beta'\nversion = '2.0.0'\n"
        )
        installed = PypiEcosystem(tmp_path).load_installed()
        names = {pkg.name for pkg in installed}
        assert "gamma" not in names
        assert "beta" in names

    def test_pin_dependency_skips_missing_dependencies_array(self, tmp_path: Path) -> None:
        """`[project]` exists without `dependencies`; the pin lives in `[dependency-groups]`."""
        from chill_out.models import FixAction

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0.1"\n[dependency-groups]\ndev = [\'requests==2.32.0\']\n'
        )
        action = FixAction(package="requests", version="2.32.3")
        fixes = PypiEcosystem(tmp_path).apply_fixes([action])
        assert len(fixes.entries) == 1
        assert fixes.entries[0].pinned_spec == "requests==2.32.3"

    @respx.mock
    async def test_fetch_version_manifest_skips_extras_marker_requirements(
        self, tmp_path: Path, http_client: httpx.AsyncClient
    ) -> None:
        """`requires_dist` rows whose marker mentions `extra` are dropped from the manifest deps."""
        respx.get(f"{PYPI_REGISTRY}/foo/1.0/json").mock(
            return_value=httpx.Response(
                200,
                json={
                    "info": {
                        "name": "foo",
                        "version": "1.0",
                        "requires_dist": [
                            "beta",
                            "gamma; extra == 'docs'",
                        ],
                    },
                },
            )
        )
        manifest = await PypiEcosystem(root=tmp_path).fetch_version_manifest("foo", "1.0", http_client)
        assert manifest is not None
        assert "gamma" not in manifest.deps
        assert "beta" in manifest.deps

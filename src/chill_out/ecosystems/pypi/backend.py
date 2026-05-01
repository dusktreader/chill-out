"""
PyPI ecosystem backend.

Reads installed packages from `uv.lock`; the lockfile is required and the
backend raises `EcosystemError` if it's missing. `pyproject.toml` is consulted
only to tell principals (direct deps) apart from transitives and to attribute
each package to its dependency groups. Talks to the PyPI JSON API for release
timestamps. Applies fixes by editing `pyproject.toml` to pin versions and
re-running `uv lock`.
"""

import glob
import json
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx
import pendulum
import tomlkit
from loguru import logger
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import ValidationError

from chill_out.constants import DependencyGroup, EcosystemKind, FixStyle
from chill_out.ecosystems.backend import Ecosystem
from chill_out.ecosystems.constants import PYPI_REGISTRY
from chill_out.ecosystems.pypi.schemas import PypiPackageResponse, PypiVersionResponse
from chill_out.ecosystems.retry import get_with_retry
from chill_out.ecosystems.version_parsing import ParsedVersion
from chill_out.exceptions import EcosystemError, RegistryError
from chill_out.models import (
    AppliedFix,
    AppliedFixes,
    FixAction,
    InstalledPackage,
    PackageInfo,
    PackageRelease,
    VersionManifest,
    WorkspaceTopology,
)
from chill_out.state import ManagedPin, PinMechanism, RemovalOutcome


class PypiEcosystem(Ecosystem):
    """Ecosystem backend for Python projects using uv + pyproject.toml."""

    kind: EcosystemKind = EcosystemKind.PYPI
    registry_url: str = PYPI_REGISTRY

    def __init__(self, root: Path) -> None:
        self.root = root

    async def fetch_package(self, name: str, http: httpx.AsyncClient) -> PackageInfo | None:
        """
        Fetch all releases and their upload timestamps for a package from PyPI.

        The PyPI JSON API returns one entry per uploaded artifact; we take the
        earliest upload time for each version as its publish date. Releases
        with no surviving uploads (empty artifact list) are dropped from the
        result. Schema validation guarantees every artifact carries at least
        one usable timestamp, so any release with at least one artifact will
        produce a `PackageRelease`.
        """
        url = f"{self.registry_url}/{name}/json"
        with RegistryError.handle_errors(f"PyPI transport error for {name}", handle_exc_class=httpx.TransportError):
            res = await get_with_retry(http, url)

        if res.status_code == 404:
            return None

        RegistryError.require_condition(
            res.status_code == 200,
            f"PyPI returned HTTP {res.status_code} for {name}",
        )

        with RegistryError.handle_errors(
            f"PyPI returned non-JSON body for {name}",
            handle_exc_class=json.JSONDecodeError,
        ):
            data = res.json()

        with RegistryError.handle_errors(
            f"PyPI returned unexpected payload shape for {name}",
            handle_exc_class=ValidationError,
        ):
            payload = PypiPackageResponse.model_validate(data)

        releases: dict[str, PackageRelease] = {}
        for ver, files in payload.releases.items():
            if not files:
                continue
            stamps = []
            for artifact in files:
                # Schema validation guarantees one of these is non-None; the
                # local binding lets the type checker see the narrowed value.
                ts = RegistryError.enforce_defined(
                    artifact.upload_time_iso_8601 or artifact.upload_time,
                    f"PyPI artifact for {name} is missing both upload_time fields",
                )
                stamps.append(pendulum.instance(ts))
            # PyPI yanks individual files; a release is "yanked" only when every
            # artifact is marked, which is what `pip` and `uv` use to skip it.
            yanked = all(artifact.yanked for artifact in files)
            releases[ver] = PackageRelease(version=ver, published=min(stamps), yanked=yanked)

        return PackageInfo(name=name, releases=releases)

    async def fetch_version_manifest(self, name: str, version: str, http: httpx.AsyncClient) -> VersionManifest | None:
        """
        Fetch the dependency declarations for a single PyPI release.

        Pulls `info.requires_dist` from the per-version JSON endpoint. Markers
        that gate a requirement on an `extra` are skipped: those represent
        optional installs and don't constrain the base resolution.
        """
        url = f"{self.registry_url}/{name}/{version}/json"
        with RegistryError.handle_errors(
            f"PyPI transport error for {name}=={version}", handle_exc_class=httpx.TransportError
        ):
            res = await get_with_retry(http, url)

        if res.status_code == 404:
            return None

        RegistryError.require_condition(
            res.status_code == 200,
            f"PyPI returned HTTP {res.status_code} for {name}=={version}",
        )

        with RegistryError.handle_errors(
            f"PyPI returned non-JSON body for {name}=={version}", handle_exc_class=json.JSONDecodeError
        ):
            data = res.json()

        with RegistryError.handle_errors(
            f"PyPI returned unexpected payload shape for {name}=={version}",
            handle_exc_class=ValidationError,
        ):
            payload = PypiVersionResponse.model_validate(data)

        deps: dict[str, str] = {}
        for raw in payload.info.requires_dist or []:
            # Schema validation already accepted the entry as a parseable
            # Requirement; re-parse here to inspect the marker and specifier.
            req = Requirement(raw)

            # Skip optional-extra requirements; they only apply when the parent
            # is installed with that extra, which we don't track.
            if req.marker is not None and "extra" in str(req.marker):
                continue

            deps[req.name] = str(req.specifier) if req.specifier else ""

        return VersionManifest(name=name, version=version, deps=deps)

    def load_installed(self) -> list[InstalledPackage]:
        """
        Enumerate every package in `uv.lock`, principals and transitives alike.

        The lockfile is the source of truth for what will actually be
        installed. Each entry becomes an `InstalledPackage` with a
        `via_chain` computed by reverse-graph BFS from the direct deps
        declared in `pyproject.toml`. Direct deps get an empty `via_chain`
        (they are principals); transitives get the shortest chain of
        intermediates back to a principal.

        Group attribution follows the same union semantic as npm: forward-walk
        from each principal and tag every reachable package with that
        principal's groups. Transitives reached through multiple principals
        accumulate the union, matching the runner's "included if reachable
        through any included group" rule.

        Requires `uv.lock` to exist; raises `EcosystemError` if it's
        missing (run `uv lock` to generate one).
        """
        direct_specs = self._read_direct_specs()
        principals = set(direct_specs.keys())

        lock_path = self.root / "uv.lock"
        EcosystemError.require_condition(
            lock_path.is_file(),
            "Cannot enumerate installed packages without uv.lock; run `uv lock` first.",
        )

        doc = tomlkit.parse(lock_path.read_text())
        packages = doc.get("package") or []

        version_by_name, deps_by_name, required_by = self._parse_lock_packages(packages)
        groups_by_pkg = self._propagate_groups(direct_specs, deps_by_name)

        return self._build_installed_packages(
            ecosystem=self.kind,
            version_by_name=version_by_name,
            required_by=required_by,
            groups_by_pkg=groups_by_pkg,
            principals=principals,
        )

    def _read_direct_specs(self) -> dict[str, tuple[str, set[DependencyGroup]]]:
        """
        Return a map of `normalized_name -> (raw_spec_string, groups)` for
        every direct dependency declared in pyproject.toml.

        Group attribution mirrors the conventional pypi layout:

        * `[project.dependencies]` -> `DependencyGroup.MAIN`
        * `[dependency-groups.dev]` and
          `[project.optional-dependencies.dev]` -> `DependencyGroup.DEV`
        * Every other `[project.optional-dependencies.*]` extra and every
          other `[dependency-groups.*]` group -> `DependencyGroup.OPTIONAL`

        A package declared in more than one section accumulates every
        matching group.
        """
        path = self.root / "pyproject.toml"
        EcosystemError.require_condition(path.is_file(), f"No pyproject.toml at {path}")
        doc = tomlkit.parse(path.read_text())

        specs: dict[str, tuple[str, set[DependencyGroup]]] = {}

        project = EcosystemError.ensure_type(
            doc.get("project", {}),
            dict,
            message="pyproject.toml [project] must be a table",
        )
        self._absorb_requirements(specs, project.get("dependencies"), DependencyGroup.MAIN)
        for extra_name, extras in (project.get("optional-dependencies") or {}).items():
            group = DependencyGroup.DEV if str(extra_name) == "dev" else DependencyGroup.OPTIONAL
            self._absorb_requirements(specs, extras, group)

        groups = EcosystemError.ensure_type(
            doc.get("dependency-groups", {}),
            dict,
            message="pyproject.toml [dependency-groups] must be a table",
        )
        for group_name, items in groups.items():
            semantic = DependencyGroup.DEV if str(group_name) == "dev" else DependencyGroup.OPTIONAL
            self._absorb_requirements(specs, items, semantic)

        return specs

    def workspace_topology(self) -> WorkspaceTopology | None:
        """
        Detect a uv workspace by walking up to find a `pyproject.toml` with `[tool.uv.workspace]`.

        Starts at `self.root` and walks toward the filesystem root until it finds a
        `pyproject.toml` declaring `[tool.uv.workspace]`. Reads `members` (glob patterns)
        and `exclude`, expands the globs, and returns a `WorkspaceTopology` keyed by each
        member's `project.name`.

        Returns `None` when no workspace declaration is reachable from `self.root`.
        """
        (workspace_root, workspace_doc) = self._locate_workspace_root(self.root)
        if workspace_root is None or workspace_doc is None:
            return None

        members_field = workspace_doc.get("members", [])
        exclude_field = workspace_doc.get("exclude", [])
        excluded = self._resolve_workspace_excludes(workspace_root, exclude_field)
        members = self._discover_workspace_members(workspace_root, members_field, excluded)

        if not members:
            return None

        return WorkspaceTopology(root=workspace_root, members=members)

    def range_satisfies(self, version: str, range_spec: str) -> bool:
        """
        Return True if `version` satisfies a PEP 440 `range_spec`.

        An empty or whitespace-only range matches any version (matches
        `packaging`'s `SpecifierSet("")` semantics). Unparsable inputs are
        treated permissively to match the original script's "assume compatible"
        behavior for transitive deps with no discoverable range.
        """
        if not range_spec.strip():
            return True

        try:
            parsed = Version(version)
        except InvalidVersion:
            logger.warning(f"Cannot parse version {version!r}; assuming range is satisfied")
            return True

        try:
            spec = SpecifierSet(range_spec)
        except InvalidSpecifier:
            logger.warning(f"Cannot parse specifier {range_spec!r}; assuming range is satisfied")
            return True

        # prereleases=True so that a candidate like 1.0.0rc1 isn't silently filtered.
        return spec.contains(parsed, prereleases=True)

    def parse_version(self, version: str) -> ParsedVersion | None:
        """
        Parse a version string with PEP 440 semantics.

        PEP 440 is a superset of semver for parsing purposes: anything
        `packaging.Version` accepts (including 2-segment releases like
        `3.12`, post-releases like `1.0.post1`, epochs like `1!2.0`,
        and dev releases) becomes a usable `ParsedVersion`. Inputs
        outside that grammar return `None`; the cooldown engine treats
        `None` as "skip this candidate" rather than raising.

        Short releases get zero-padded for the major / minor / micro view:
        `3.12` reports `major=3, minor=12, micro=0` so the engine
        classifies it as a minor release. Versions with more than three
        release segments truncate to the first three for classification but
        keep the full release tuple in the sort key, so `1.2.3.4` still
        sorts after `1.2.3` the way packaging compares it.

        The original string is preserved verbatim so safe versions
        round-trip back through fix actions in the exact form the registry
        published, even when packaging would canonicalize it differently
        (e.g. `2.0.0-rc1` -> `2.0.0rc1`).

        The `sort_key` wraps `packaging.Version` in a single-element tuple.
        `Version` already implements PEP 440 ordering directly (epochs first,
        then release, then pre-release, then post-release, then dev-release);
        the tuple wrapper exists so the `ParsedVersion.sort_key` contract
        stays uniform across ecosystems.
        """
        try:
            v = Version(version)
        except InvalidVersion:
            return None

        release = v.release
        major = release[0] if len(release) > 0 else 0
        minor = release[1] if len(release) > 1 else 0
        micro = release[2] if len(release) > 2 else 0

        return ParsedVersion(
            original=version,
            major=major,
            minor=minor,
            micro=micro,
            is_prerelease=v.is_prerelease,
            sort_key=(v,),
        )

    def apply_fixes(self, actions: list[FixAction]) -> AppliedFixes:
        """
        Apply pins. Routes `via_overrides` actions through `apply_override_fixes`.

        Direct pins are written into `self.root`'s `pyproject.toml` and
        validated with `uv lock`. Override pins go through the workspace
        root's `[tool.uv].override-dependencies` field and trigger a
        workspace-wide `uv lock` to recompute the resolution.
        """
        if not actions:
            return AppliedFixes()

        direct_actions = [a for a in actions if not a.via_overrides]
        override_actions = [a for a in actions if a.via_overrides]

        result = AppliedFixes()
        if direct_actions:
            direct_result = self._apply_direct_fixes(direct_actions)
            result.entries.extend(direct_result.entries)
            result.log.extend(direct_result.log)

        if override_actions:
            override_result = self.apply_override_fixes(override_actions)
            if override_result is None:
                logger.warning("override path unavailable; falling back to direct pins for shared actions")
                fallback = self._apply_direct_fixes(override_actions)
                result.entries.extend(fallback.entries)
                result.log.extend(fallback.log)
            else:
                result.entries.extend(override_result.entries)
                result.log.extend(override_result.log)

        return result

    def _apply_direct_fixes(self, actions: list[FixAction]) -> AppliedFixes:
        """
        Apply direct pins to `self.root`'s `pyproject.toml` and re-lock.

        For each action, `_pin_dependency` walks `[project.dependencies]`,
        `[project.optional-dependencies]`, and `[dependency-groups.*]` looking
        for an existing entry that names the package. If one is found, it's
        rewritten in place and the new spec string is recorded. If no
        existing entry exists anywhere, the package is appended to
        `[project.dependencies]` as a fresh declaration so the pin has
        somewhere to live.

        After all edits are written back to disk, `uv lock` runs to validate
        the resulting manifest and refresh `uv.lock`. A non-zero exit from
        `uv lock` raises `EcosystemError` with the captured stderr; partial
        edits are left on disk in that case so the user can see what was
        attempted.

        The `manifest_path` recorded on every `AppliedFix` is always
        `"pyproject.toml"` (project-relative) since direct pins only ever
        touch the project's own manifest.
        """
        pyproject_path = self.root / "pyproject.toml"
        EcosystemError.require_condition(pyproject_path.is_file(), f"No pyproject.toml at {pyproject_path}")

        doc = tomlkit.parse(pyproject_path.read_text())
        result = AppliedFixes()

        for action in actions:
            replacement = self._pin_dependency(doc, action.package, action.version, action.style)
            if replacement is not None:
                result.log.append(f"pinned {action.package} -> {replacement}")
                result.entries.append(
                    AppliedFix(
                        action=action,
                        pinned_spec=replacement,
                        via_overrides=False,
                        manifest_path=Path("pyproject.toml"),
                    )
                )
            else:
                # Add to project.dependencies if it isn't declared anywhere.
                project = doc.setdefault("project", tomlkit.table())
                deps = project.setdefault("dependencies", tomlkit.array())
                fresh = self._format_pypi_spec(action.package, action.version, None, action.style)
                deps.append(fresh)
                result.log.append(f"added {fresh} to project.dependencies")
                result.entries.append(
                    AppliedFix(
                        action=action,
                        pinned_spec=fresh,
                        via_overrides=False,
                        manifest_path=Path("pyproject.toml"),
                    )
                )

        pyproject_path.write_text(tomlkit.dumps(doc))

        proc = subprocess.run(["uv", "lock"], cwd=self.root, capture_output=True, text=True)
        EcosystemError.require_condition(
            proc.returncode == 0,
            f"`uv lock` failed after applying fixes: {proc.stderr.strip()}",
        )
        result.log.append("ran: uv lock")
        return result

    def remove_managed_pin(self, pin: ManagedPin) -> RemovalOutcome:
        """
        Reverse a previously-applied managed pin from the project's pyproject.toml.

        For `PinMechanism.DIRECT` this removes the entry from `[project.dependencies]`,
        `[project.optional-dependencies]`, or `[dependency-groups.*]` (whichever holds it).
        For `PinMechanism.OVERRIDE` this removes the entry from
        `[tool.uv.override-dependencies]` at the recorded manifest path.

        See `Ecosystem.remove_managed_pin` for outcome semantics.
        """
        path = self.root / pin.manifest_path
        if not path.is_file():
            return RemovalOutcome.ORPHAN

        doc = tomlkit.parse(path.read_text())
        if pin.mechanism is PinMechanism.OVERRIDE:
            outcome = self._remove_pypi_override_entry(doc, pin)
        else:
            outcome = self._remove_pypi_direct_entry(doc, pin)

        if outcome is RemovalOutcome.REMOVED:
            path.write_text(tomlkit.dumps(doc))

        return outcome

    def regenerate_lockfile(self) -> str:
        """Recompute `uv.lock` by running `uv lock` from the project root."""
        proc = subprocess.run(["uv", "lock"], cwd=self.root, capture_output=True, text=True)
        EcosystemError.require_condition(
            proc.returncode == 0,
            f"`uv lock` failed during lockfile regeneration: {proc.stderr.strip()}",
        )
        return "ran: uv lock"

    def supports_overrides(self) -> bool:
        return True

    def apply_override_fixes(self, actions: list[FixAction]) -> AppliedFixes | None:
        """
        Force transitive versions via uv's `override-dependencies` mechanism.

        Writes one entry per action to `[tool.uv].override-dependencies`
        in the workspace root's `pyproject.toml` (or `self.root` when
        there's no workspace), then runs `uv lock` from that directory
        to recompute the workspace-wide resolution. Returns an
        `AppliedFixes` on success, or `None` when no usable workspace
        root could be located.
        """
        if not actions:
            return AppliedFixes()

        topology = self.workspace_topology()
        write_root = topology.root if topology is not None else self.root

        path = write_root / "pyproject.toml"
        if not path.is_file():
            return None

        # When write_root sits above self.root (workspace ancestor case), `path` isn't
        # relative to the project root and we record the absolute path instead so cleanup
        # can still find it.
        if path.is_relative_to(self.root):
            manifest_path = path.relative_to(self.root)
        else:
            manifest_path = path

        doc = tomlkit.parse(path.read_text())
        tool = doc.setdefault("tool", tomlkit.table())
        uv_section = tool.setdefault("uv", tomlkit.table())
        existing_overrides = uv_section.get("override-dependencies", [])

        result = AppliedFixes()
        # Drop any existing entries we're replacing so we don't accumulate
        # duplicate pins on re-runs.
        kept: list[str] = []
        for raw in existing_overrides:
            try:
                req = Requirement(str(raw))
                replaced = any(self._normalize(req.name) == self._normalize(a.package) for a in actions)
            except InvalidRequirement:
                replaced = False
            if not replaced:
                kept.append(str(raw))

        # Rebuild the array fresh with the kept entries plus the new overrides.
        overrides = tomlkit.array()
        uv_section["override-dependencies"] = overrides
        for raw in kept:
            overrides.append(raw)
        for action in actions:
            spec = f"{action.package}=={action.version}"
            overrides.append(spec)
            result.log.append(f"overrode {spec} (workspace root)")
            result.entries.append(
                AppliedFix(
                    action=action,
                    pinned_spec=spec,
                    via_overrides=True,
                    manifest_path=manifest_path,
                )
            )

        path.write_text(tomlkit.dumps(doc))

        proc = subprocess.run(["uv", "lock"], cwd=write_root, capture_output=True, text=True)
        EcosystemError.require_condition(
            proc.returncode == 0,
            f"`uv lock` failed after applying overrides: {proc.stderr.strip()}",
        )
        result.log.append(f"ran: uv lock (in {write_root})")
        return result

    @staticmethod
    def _normalize(name: str) -> str:
        """PEP 503 name normalization."""
        return re.sub(r"[-_.]+", "-", name).lower()

    @staticmethod
    def _absorb_requirements(
        specs: dict[str, tuple[str, set[DependencyGroup]]],
        items: Any,
        group: DependencyGroup,
    ) -> None:
        """
        Fold a `pyproject.toml` requirement array into the running `specs` map.

        `_read_direct_specs` walks several requirement arrays (the main
        `[project.dependencies]` list, every `[project.optional-dependencies.*]`
        extra, and every `[dependency-groups.*]` group). Each array contributes
        the same kind of records to the same accumulator, so this helper does
        the actual folding: parse each entry as a PEP 508 `Requirement`,
        normalize its name, and either insert a new `(raw_spec, {group})`
        record or union the new `group` into an existing one.

        Mutation-in-place is deliberate: `specs` is the caller's accumulator
        across all the arrays it iterates, and threading it back through a
        return value would force every call site to reassign. Returning
        `None` keeps the call shape symmetric across the three sources.

        Behavior worth knowing:

        - `items` may be `None` or an empty array (the section was absent or
          declared empty in `pyproject.toml`); both short-circuit silently.
        - Entries that don't parse as a `Requirement` log a warning and are
          skipped. They don't fail the whole load: `pyproject.toml` is
          user-edited and a single malformed line shouldn't prevent the rest
          of the deps from being analyzed.
        - The first occurrence of a package wins for the `raw_spec` field;
          later occurrences only contribute their `group`. This matches the
          semantics chill-out wants downstream: the spec string is only used
          for display and pin-rewriting, where any of the user's declarations
          is equally valid, but the group set must be the union to capture
          every section that mentions the package.
        """
        if not items:
            return
        for raw in items:
            try:
                req = Requirement(str(raw))
            except InvalidRequirement:
                logger.warning(f"Skipping unparsable requirement: {raw!r}")
                continue
            normalized = PypiEcosystem._normalize(req.name)
            existing = specs.get(normalized)
            if existing is None:
                specs[normalized] = (str(raw), {group})
            else:
                existing[1].add(group)

    @staticmethod
    def _locate_workspace_root(start: Path) -> tuple[Path | None, dict[str, Any] | None]:
        """
        Walk upward from `start` looking for a `pyproject.toml` with `[tool.uv.workspace]`.

        Begins at `start.resolve()` and walks parent-by-parent until a directory
        is found whose `pyproject.toml` declares a `[tool.uv.workspace]` table.
        Returns the `(workspace_root, workspace_table)` tuple from that
        directory, or `None` when the walk reaches the filesystem root without
        finding a declaration.

        The walk stops when `current == current.parent`, which is the standard
        idiom for "we hit the filesystem root". `Path.resolve()` is called once
        on the starting point so symlinks and `..` segments are collapsed
        before the walk begins; subsequent `parent` traversal stays in resolved
        form.

        Behavior worth knowing:

        - A `pyproject.toml` that doesn't parse as TOML raises `EcosystemError`
          via the `handle_errors` block. A typo in a parent `pyproject.toml`
          shouldn't be silently swallowed during workspace detection.
        - A `pyproject.toml` that parses but has no `[tool.uv.workspace]`
          table is fine; the walk continues upward.
        - A `[tool.uv.workspace]` value that exists but isn't a table (e.g.
          a string or array) raises `EcosystemError` via `ensure_type`.
          Returning `None` for malformed-but-present declarations would
          silently mask user errors.
        - Directories without a `pyproject.toml` at all are skipped without
          comment; that's the common case at every level above the project.
        """
        current = start.resolve()
        while current != current.parent:
            candidate = current / "pyproject.toml"
            if candidate.is_file():
                with EcosystemError.handle_errors(f"Malformed pyproject.toml found at {candidate}"):
                    doc = tomlkit.parse(candidate.read_text())
                workspace_table = doc.get("tool", {}).get("uv", {}).get("workspace")
                if workspace_table:
                    workspace_table = EcosystemError.ensure_type(
                        workspace_table,
                        dict,
                        message=f"Malformed [tool.uv.workspace] in {candidate}",
                    )
                    return (current, workspace_table)
            current = current.parent
        return (None, None)

    @staticmethod
    def _resolve_workspace_excludes(workspace_root: Path, exclude_patterns: Iterable[str]) -> set[Path]:
        """
        Expand `[tool.uv.workspace].exclude` glob patterns to a set of resolved paths.

        Each pattern is joined with `workspace_root` and passed to `glob.glob`,
        so the patterns themselves can use any of the wildcards `glob` supports
        (`*`, `?`, `**` with `recursive=True` semantics not enabled here, etc).
        Every match is resolved (symlinks followed, `.` and `..` collapsed)
        so equality checks against `member_dir.resolve()` in
        `_discover_workspace_members` work correctly even when the user
        declares members and excludes through different path forms.

        Patterns that match nothing contribute nothing; that's identical to
        the user not having declared them. Returning a `set` keeps the
        downstream `member.resolve() in excluded` check O(1) per member.
        """
        excluded: set[Path] = set()
        for pattern in exclude_patterns:
            for match in glob.glob(str(workspace_root / pattern)):
                excluded.add(Path(match).resolve())
        return excluded

    @staticmethod
    def _discover_workspace_members(
        workspace_root: Path,
        member_patterns: Iterable[str],
        excluded: set[Path],
    ) -> dict[str, Path]:
        """
        Resolve `[tool.uv.workspace].members` glob patterns to a name -> directory map.

        Each pattern is expanded relative to `workspace_root`, then every match
        is filtered through several gates before becoming a workspace member:

        - Must be a directory. `glob.glob` happily returns files too; uv
          workspaces only mean directories with a `pyproject.toml`.
        - Must not be in `excluded` (the resolved-path set produced by
          `_resolve_workspace_excludes`). The match is `resolve`d before the
          check so that symlinks and relative-path forms compare correctly.
        - Must contain a readable `pyproject.toml`. Member candidates without
          one are silently skipped; uv treats those as not-yet-initialized
          directories rather than errors.
        - The member's `pyproject.toml` must declare `project.name`. Without
          a name there's no key to slot the member under in the topology.

        Members that pass all the gates are keyed in the result by their
        PEP 503-normalized `project.name`. The value is the unmodified
        `Path` returned by `glob` (not resolved), to preserve the caller's
        view of the workspace layout.

        A member's `pyproject.toml` that exists but doesn't parse as TOML
        raises `EcosystemError` via the `handle_errors` block; that's a
        deliberate corruption-vs-misconfiguration split that matches the
        rest of the backend.
        """
        members: dict[str, Path] = {}
        for pattern in member_patterns:
            for match in glob.glob(str(workspace_root / pattern)):
                member_dir = Path(match)
                if not member_dir.is_dir():
                    continue
                if member_dir.resolve() in excluded:
                    continue
                member_pyproject = member_dir / "pyproject.toml"
                if not member_pyproject.is_file():
                    continue
                with EcosystemError.handle_errors(f"Malformed member pyproject.toml found at {member_pyproject}"):
                    member_doc = tomlkit.parse(member_pyproject.read_text())
                project_name = member_doc.get("project", {}).get("name")
                if not project_name:
                    continue
                members[PypiEcosystem._normalize(str(project_name))] = member_dir
        return members

    @staticmethod
    def _find_via_chain(
        name: str,
        required_by: dict[str, set[str]],
        principals: set[str],
    ) -> tuple[str, ...]:
        """
        Compute the shortest dependency chain from a transitive back to a principal.

        `uv.lock` stores dependency edges as forward links (each `[[package]]`
        declares the names it requires). To attribute a transitive package to the
        direct dependency that pulled it in, we need to walk the graph in the
        opposite direction. `required_by` is the precomputed reverse graph,
        mapping each package name to the set of packages that declare it.

        The walk is a breadth-first search starting at `name`, following
        `required_by` edges back toward the roots. The first principal it
        reaches gives the shortest chain (BFS guarantees shortest-path on an
        unweighted graph). The returned tuple is the chain of intermediate
        packages between `name` and that principal, with the immediate
        parent first and the principal itself last; `name` is not included.

        Examples (with `principals = {"app"}`):

        - Direct dep edge `app -> foo`: `_find_via_chain("foo", ...)` returns
          `("app",)`.
        - Two-hop chain `app -> foo -> bar`: `_find_via_chain("bar", ...)`
          returns `("foo", "app")`.
        - Diamond where `app -> a -> z` and `app -> b -> z`, both length two:
          one of `("a", "app")` or `("b", "app")` wins; BFS makes the choice
          deterministic for a given iteration order over `required_by`, but
          callers should not rely on which side is picked.

        Returns an empty tuple when `name` is not in `required_by` (orphan
        package with no parents) or when no path back to any principal exists
        (disconnected component, which only happens with malformed lockfiles).
        Calling this with `name in principals` is wasteful but harmless; the
        BFS will return `()` immediately because the principal-check skips the
        starting node.
        """
        visited = {name}
        prev: dict[str, str] = {}
        queue = [name]
        while queue:
            node = queue.pop(0)
            if node in principals and node != name:
                path = []
                cur = node
                while cur != name:
                    path.append(cur)
                    cur = prev[cur]
                path.reverse()
                return tuple(path)
            for parent in required_by.get(node, ()):
                if parent not in visited:
                    visited.add(parent)
                    prev[parent] = node
                    queue.append(parent)
        return ()

    @staticmethod
    def _propagate_groups(
        direct_specs: dict[str, tuple[str, set[DependencyGroup]]],
        deps_by_name: dict[str, set[str]],
    ) -> dict[str, set[DependencyGroup]]:
        """
        Propagate dependency-group labels from each principal to its transitives.

        Each principal package carries a set of `DependencyGroup` labels that
        indicate which sections of `pyproject.toml` declared it (e.g. main
        dependencies, an optional extra, a dev group). Transitively-installed
        packages don't carry their own labels in `uv.lock`, so chill-out attributes
        them by walking the forward dependency graph from each principal and
        tagging every reachable package with the principal's labels.

        The walk is performed once per principal, as an independent breadth-first
        search starting at the principal and following `deps_by_name` edges. Each
        visited node has the principal's label set unioned into its accumulated
        label set in the result. This per-principal seeding is what makes labels
        from different principals accumulate naturally: if package `foo` is
        reachable from both a main-deps principal and a dev-deps principal, its
        final label set contains both groups.

        `direct_specs` maps principal name to a `(spec_string, groups)` tuple;
        only the groups are used here (the spec string is consumed elsewhere in
        `load_installed`). `deps_by_name` is the forward graph: each package name
        mapped to the set of names it directly requires. Packages not reachable
        from any principal are absent from the returned dict; callers should
        treat a missing entry as the empty set.

        Cycles in the forward graph are bounded per-principal by the `visited`
        set, so a malformed lockfile cannot cause an infinite loop.
        """
        groups_by_pkg: dict[str, set[DependencyGroup]] = {}
        for principal_name, (_, dep_groups) in direct_specs.items():
            visited = {principal_name}
            queue = [principal_name]
            while queue:
                node = queue.pop(0)
                groups_by_pkg.setdefault(node, set()).update(dep_groups)
                for child in deps_by_name.get(node, ()):
                    if child not in visited:
                        visited.add(child)
                        queue.append(child)
        return groups_by_pkg

    @staticmethod
    def _parse_lock_packages(
        packages: Iterable[Any],
    ) -> tuple[dict[str, str], dict[str, set[str]], dict[str, set[str]]]:
        """
        Walk `[[package]]` entries from `uv.lock` into the three graph dicts.

        `uv.lock` stores a flat list of `[[package]]` tables, each with a `name`,
        a `version`, and a `dependencies` list of `{name = ...}` entries pointing
        at other packages in the same lockfile. To analyze the dependency graph
        we need three derived views, all keyed by PEP 503-normalized package name:

        - `version_by_name`: every locked package mapped to its resolved version.
        - `deps_by_name`: the forward graph; each package mapped to the set of
          names it directly requires.
        - `required_by`: the reverse graph; each package mapped to the set of
          names that declare it as a dependency.

        All three are built in a single pass to avoid walking the package list
        multiple times. The same pass enforces lockfile invariants: a package
        entry with a missing `version`, or a dependency entry with a missing
        `name`, raises `EcosystemError`. These shouldn't occur in lockfiles
        produced by `uv lock`, but hand-edited or schema-drifted files surface
        here with a clear message instead of silently corrupting the graph.

        Package entries with no `name` field are skipped silently rather than
        raising; tomlkit may surface comment-only or otherwise nameless tables
        that are not actual package declarations.

        `packages` is the raw value from `doc.get("package")` after a tomlkit
        parse, typed loosely as `Iterable[Any]` because tomlkit's container
        types resist precise typing. Each element is expected to support
        `.get("name")`, `.get("version")`, and `.get("dependencies")`.
        """
        version_by_name: dict[str, str] = {}
        deps_by_name: dict[str, set[str]] = {}
        required_by: dict[str, set[str]] = {}

        for pkg in packages:
            name = PypiEcosystem._normalize(pkg.get("name", ""))
            if not name:
                continue

            version = pkg.get("version")
            EcosystemError.require_condition(version, f"uv.lock entry for {name!r} is missing a version")
            version_by_name[name] = version

            deps: set[str] = set()
            for dep in pkg.get("dependencies") or []:
                dep_name = PypiEcosystem._normalize(
                    EcosystemError.enforce_defined(
                        dep.get("name"),
                        f"uv.lock dependency entry under {name!r} is missing a name",
                    )
                )
                deps.add(dep_name)
                required_by.setdefault(dep_name, set()).add(name)

            deps_by_name[name] = deps

        return version_by_name, deps_by_name, required_by

    @staticmethod
    def _build_installed_packages(
        *,
        ecosystem: EcosystemKind,
        version_by_name: dict[str, str],
        required_by: dict[str, set[str]],
        groups_by_pkg: dict[str, set[DependencyGroup]],
        principals: set[str],
    ) -> list[InstalledPackage]:
        """
        Assemble `InstalledPackage` records from the parsed lockfile graph.

        This is the join step that ties together the three derived views built
        by `_parse_lock_packages` and `_propagate_groups`. For each entry in
        `version_by_name`, it produces one `InstalledPackage` carrying:

        - The PEP 503-normalized name and resolved version.
        - The ecosystem identity (always `EcosystemKind.PYPI` in the current
          backend, but passed in as a parameter so this helper is reusable and
          testable without instantiating an ecosystem class).
        - The `via_chain` attributing transitives back to a principal: empty
          for principals themselves, otherwise the result of the reverse-BFS
          in `_find_via_chain`.
        - The deterministic, alphabetically-sorted tuple of `DependencyGroup`
          labels collected by `_propagate_groups`. Sorting by `group.value`
          gives stable output independent of set iteration order, which keeps
          downstream snapshots and rendered tables reproducible.

        Packages absent from `groups_by_pkg` get an empty `groups` tuple; that
        happens when a transitive is reachable in the lockfile graph but no
        principal walks to it (which shouldn't occur with a well-formed lock,
        but the assembly tolerates it rather than raising).
        """
        out: list[InstalledPackage] = []
        for name, version in version_by_name.items():
            via_chain = () if name in principals else PypiEcosystem._find_via_chain(name, required_by, principals)
            pkg_groups = tuple(sorted(groups_by_pkg.get(name, set()), key=lambda g: g.value))
            out.append(
                InstalledPackage(
                    name=name,
                    version=version,
                    ecosystem=ecosystem,
                    via_chain=via_chain,
                    groups=pkg_groups,
                )
            )
        return out

    @staticmethod
    def _remove_pypi_direct_entry(doc: Any, pin: ManagedPin) -> RemovalOutcome:
        """Find and remove a direct-dependency entry for `pin.package` from `doc`.

        Walks `[project.dependencies]`, `[project.optional-dependencies.*]`, and
        `[dependency-groups.*]` looking for a requirement whose normalized name matches the pin's
        package. Returns REMOVED if the value matches `pin.pinned_spec`, DRIFTED if the entry exists
        with a different value, or ORPHAN if no matching entry is found anywhere.
        """
        target = PypiEcosystem._normalize(pin.package)

        def search_and_remove(items: Any) -> RemovalOutcome | None:
            if items is None:
                return None
            for idx in range(len(items)):
                raw = str(items[idx])
                try:
                    req = Requirement(raw)
                except InvalidRequirement:
                    continue
                if PypiEcosystem._normalize(req.name) != target:
                    continue
                if raw == pin.pinned_spec:
                    del items[idx]
                    return RemovalOutcome.REMOVED
                return RemovalOutcome.DRIFTED
            return None

        project = doc.get("project")
        if isinstance(project, dict):
            outcome = search_and_remove(project.get("dependencies"))
            if outcome is not None:
                return outcome
            for extras in (project.get("optional-dependencies") or {}).values():
                outcome = search_and_remove(extras)
                if outcome is not None:
                    return outcome

        groups = doc.get("dependency-groups")
        if isinstance(groups, dict):
            for items in groups.values():
                outcome = search_and_remove(items)
                if outcome is not None:
                    return outcome

        return RemovalOutcome.ORPHAN

    @staticmethod
    def _remove_pypi_override_entry(doc: Any, pin: ManagedPin) -> RemovalOutcome:
        """Find and remove an override entry for `pin.package` from `[tool.uv.override-dependencies]`.

        Returns REMOVED if the array contains an entry whose value matches `pin.pinned_spec`,
        DRIFTED if a same-named entry exists with a different value, or ORPHAN if no matching entry
        is found.
        """
        tool = doc.get("tool")
        if not isinstance(tool, dict):
            return RemovalOutcome.ORPHAN
        uv_section = tool.get("uv")
        if not isinstance(uv_section, dict):
            return RemovalOutcome.ORPHAN
        overrides = uv_section.get("override-dependencies")
        if overrides is None:
            return RemovalOutcome.ORPHAN

        target = PypiEcosystem._normalize(pin.package)
        for idx in range(len(overrides)):
            raw = str(overrides[idx])
            try:
                req = Requirement(raw)
            except InvalidRequirement:
                continue
            if PypiEcosystem._normalize(req.name) != target:
                continue
            if raw == pin.pinned_spec:
                del overrides[idx]
                return RemovalOutcome.REMOVED
            return RemovalOutcome.DRIFTED

        return RemovalOutcome.ORPHAN

    @staticmethod
    def _pin_dependency(doc: Any, package: str, version: str, style: FixStyle) -> str | None:
        """
        Replace any existing requirement string for `package` in either
        `project.dependencies`, `project.optional-dependencies`, or
        `dependency-groups` with a new spec built from `version` and
        `style`.

        Returns the new spec string when something was replaced, or `None`
        when no entry for `package` was found. The first replacement's
        formatted spec is returned; later replacements (if the package appears
        in multiple sections) reuse the same shape.

        For `FixStyle.COMPATIBLE`, the existing entry's lower bound is
        preserved when present so the user's declared floor isn't accidentally
        raised; otherwise `>={version}` is used as the new floor. The upper
        bound is always the next major (`<{M+1}.0.0`) under the safe version.
        """
        target = PypiEcosystem._normalize(package)
        replaced_spec: str | None = None

        def rewrite(items: Any) -> None:
            nonlocal replaced_spec
            if items is None:
                return
            for idx in range(len(items)):
                raw = str(items[idx])
                try:
                    req = Requirement(raw)
                except InvalidRequirement:
                    continue
                if PypiEcosystem._normalize(req.name) != target:
                    continue
                new_spec = PypiEcosystem._format_pypi_spec(package, version, req.specifier, style)
                items[idx] = new_spec
                if replaced_spec is None:
                    replaced_spec = new_spec

        project = doc.get("project")
        if isinstance(project, dict):
            rewrite(project.get("dependencies"))
            for extras in (project.get("optional-dependencies") or {}).values():
                rewrite(extras)

        groups = doc.get("dependency-groups")
        if isinstance(groups, dict):
            for items in groups.values():
                rewrite(items)

        return replaced_spec

    @staticmethod
    def _format_pypi_spec(
        package: str,
        version: str,
        existing: SpecifierSet | None,
        style: FixStyle,
    ) -> str:
        """Render the new requirement string for a pypi pin.

        For `FixStyle.EXACT` the result is `{package}=={version}`.

        For `FixStyle.COMPATIBLE` the result is
        `{package}>={lower},<{M+1}.0.0` where `M` is the safe version's
        major component. `lower` is the highest `>=` bound the user already
        declared (only kept when it doesn't exceed `version`); otherwise it
        falls back to `version` itself.
        """
        if style is FixStyle.EXACT:
            return f"{package}=={version}"

        try:
            safe = Version(version)
        except InvalidVersion:
            # Can't reason about the version structure; fall back to exact.
            return f"{package}=={version}"

        upper = f"<{safe.major + 1}.0.0"
        lower_str = PypiEcosystem._existing_lower_bound(existing, safe)
        if lower_str is None:
            lower_str = version
        return f"{package}>={lower_str},{upper}"

    @staticmethod
    def _existing_lower_bound(spec: SpecifierSet | None, safe: Version) -> str | None:
        """
        Return the highest `>=` lower bound declared in `spec` that is
        still `<= safe`. Returns `None` when no usable lower bound exists.

        Bounds higher than the safe version would forbid the safe version
        itself, so they're discarded -- the caller falls back to using the
        safe version as the floor instead.
        """
        if spec is None:
            return None
        candidates: list[Version] = []
        for sp in spec:
            if sp.operator != ">=":
                continue
            try:
                v = Version(sp.version)
            except InvalidVersion:  # pragma: no cover - SpecifierSet validates versions upfront
                continue
            if v <= safe:
                candidates.append(v)
        if not candidates:
            return None
        return str(max(candidates))

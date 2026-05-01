"""
npm ecosystem backend.

Reads installed packages from `npm list --json` and from `package-lock.json`
for transitive resolution. Talks to the npm registry. Applies fixes by editing
the root `package.json` to pin every safe version (direct or promoted-from-
transitive) into `dependencies`, then re-running `npm install`.
"""

import glob
import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pendulum
import semver
from loguru import logger
from pydantic import ValidationError

from chill_out.constants import DependencyGroup, EcosystemKind, FixStyle
from chill_out.ecosystems.backend import Ecosystem
from chill_out.ecosystems.constants import NPM_REGISTRY, NPM_SECTION_GROUPS
from chill_out.ecosystems.npm.schemas import NpmPackageResponse, NpmVersionResponse
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

# Top-level keys in the npm registry's `time` map that aren't versions; the
# registry slips a `created` and `modified` field in alongside the per-version
# timestamps and we have to filter them back out before building releases.
_NPM_TIME_BOOKKEEPING = frozenset({"created", "modified"})


class NpmEcosystem(Ecosystem):
    """Ecosystem backend for npm projects."""

    kind: EcosystemKind = EcosystemKind.NPM
    registry_url: str = NPM_REGISTRY

    def __init__(self, root: Path) -> None:
        self.root = root

    async def fetch_package(self, name: str, http: httpx.AsyncClient) -> PackageInfo | None:
        """
        Fetch all release timestamps for a package from the npm registry.

        Returns `None` if the package is missing (404). Raises `RegistryError`
        on transport failures, non-2xx responses other than 404, non-JSON
        bodies, or any drift in the response shape that fails Pydantic
        validation.
        """
        url = f"{self.registry_url}/{name}"
        with RegistryError.handle_errors(
            f"npm registry transport error for {name}", handle_exc_class=httpx.TransportError
        ):
            res = await get_with_retry(http, url)

        if res.status_code == 404:
            return None

        RegistryError.require_condition(
            res.status_code == 200,
            f"npm registry returned HTTP {res.status_code} for {name}",
        )

        with RegistryError.handle_errors(
            f"npm registry returned non-JSON body for {name}", handle_exc_class=json.JSONDecodeError
        ):
            data = res.json()

        with RegistryError.handle_errors(
            f"npm registry returned unexpected payload shape for {name}",
            handle_exc_class=ValidationError,
        ):
            payload = NpmPackageResponse.model_validate(data)

        releases: dict[str, PackageRelease] = {
            ver: PackageRelease(
                version=ver,
                published=pendulum.instance(ts),
                # npm keeps unpublished versions in `time` for history but
                # drops them from `versions`. Anything in `time` and not in
                # `versions` has been withdrawn from the registry. The
                # `versions` map is only consulted for unpublish detection
                # when it's present at all -- a response that omits it
                # entirely (some abbreviated formats do) leaves yank status
                # as "unknown but not flagged," matching the conservative
                # default on `PackageRelease`.
                yanked=bool(payload.versions) and ver not in payload.versions,
            )
            for ver, ts in payload.time.items()
            if ver not in _NPM_TIME_BOOKKEEPING
        }
        return PackageInfo(name=name, releases=releases)

    async def fetch_version_manifest(self, name: str, version: str, http: httpx.AsyncClient) -> VersionManifest | None:
        """
        Fetch dependency declarations for `{name}@{version}` from the npm registry.

        Returns `None` for 404 responses. Merges `dependencies` and
        `peerDependencies` into a single `deps` map; npm treats peer deps as
        runtime constraints just like regular deps for resolution purposes,
        so the cooldown engine should see both when checking whether a safe
        transitive can be hoisted.
        """
        url = f"{self.registry_url}/{name}/{version}"
        with RegistryError.handle_errors(
            f"npm registry transport error for {name}@{version}", handle_exc_class=httpx.TransportError
        ):
            res = await get_with_retry(http, url)

        if res.status_code == 404:
            return None

        RegistryError.require_condition(
            res.status_code == 200,
            f"npm registry returned HTTP {res.status_code} for {name}@{version}",
        )

        with RegistryError.handle_errors(
            f"npm registry returned non-JSON body for {name}@{version}", handle_exc_class=json.JSONDecodeError
        ):
            data = res.json()

        with RegistryError.handle_errors(
            f"npm registry returned unexpected payload shape for {name}@{version}",
            handle_exc_class=ValidationError,
        ):
            payload = NpmVersionResponse.model_validate(data)

        deps: dict[str, str] = {**payload.dependencies, **payload.peerDependencies}
        return VersionManifest(name=name, version=version, deps=deps)

    def load_installed(self) -> list[InstalledPackage]:
        """
        Load the full dependency tree from `npm list`.

        The returned list contains every package npm reports as installed,
        principals (top-level installs) and transitives alike. Each
        `InstalledPackage` is keyed by `(name, version)` because npm
        routinely installs multiple copies of the same package at different
        versions in different branches of `node_modules`; each copy actually
        loads at runtime for whichever code requires it, so we report them
        independently.

        The work happens in three phases that mirror the pypi backend's
        shape:

        1. Run `npm list` to materialize the dependency tree, optionally
           also at the workspace root for cross-member ownership attribution.
        2. Read the project's own `package.json` to learn which top-level
           names belong to which semantic group.
        3. Walk the tree once per top-level entry to attribute every
           reachable `(name, version)` to its principal's groups, then a
           second walk to assemble the actual `InstalledPackage` records
           with their `via_chain` and ownership metadata.
        """
        data = self._npm_list(depth=None)

        # Compute cross-member ownership by running `npm list` at the
        # workspace root. When `self.root` is a workspace member, npm
        # scopes its own output to that member's subtree, which would
        # misattribute every install to a single owner. Walking the
        # lockfile-rooted tree gives us the full picture: each top-level
        # `file:`-resolved entry is a workspace member and its subtree
        # shows everything that member pulls in. In a non-workspace
        # context this falls back to the already-loaded data and the
        # index ends up empty.
        ownership_data = self._npm_list_at_workspace_root() or data
        ownership = self._compute_member_ownership(ownership_data)

        # If we're inside a workspace member, `npm list` walks up to the
        # workspace root and reports every member's tree. Scope down to
        # just this member's subtree so we don't surface (and try to
        # fix) packages that belong to a sibling member.
        member_node = self._find_workspace_member(data)
        if member_node is not None:
            data = member_node

        groups_by_name = self._read_root_package_json()
        groups_by_install = self._attribute_groups_to_installs(data, groups_by_name)
        return self._collect_installed_packages(
            data=data,
            ecosystem=self.kind,
            ownership=ownership,
            groups_by_install=groups_by_install,
        )

    def _read_root_package_json(self) -> dict[str, set[DependencyGroup]]:
        """
        Read the root `package.json` and return a per-dependency group map.

        The returned mapping is keyed by package name; each value is the
        set of semantic groups the package is declared in. A single
        package can appear in multiple sections (e.g. both `dependencies`
        and `peerDependencies`) and gets every matching group.

        Workspaces and nested `package.json` files are intentionally
        ignored; for monorepos, run chill-out from each sub-project's
        directory.

        Behavior worth knowing:

        - A missing root `package.json` returns an empty mapping; the
          caller falls back to its conservative `MAIN`-by-default
          behavior for any top-level it can't look up here.
        - An unreadable (non-JSON) root `package.json` logs a warning
          and also returns an empty mapping. This is deliberate: a
          single broken file at the project root shouldn't prevent the
          rest of the analysis from running. Errors that should be
          fatal (like missing `node_modules`) surface elsewhere with
          stronger guarantees.
        """
        groups_by_name: dict[str, set[DependencyGroup]] = {}
        root_pkg = self.root / "package.json"
        if not root_pkg.is_file():
            return groups_by_name
        try:
            doc = json.loads(root_pkg.read_text())
        except json.JSONDecodeError:
            logger.warning(f"Skipping unreadable package.json: {root_pkg}")
            return groups_by_name
        for section, group in NPM_SECTION_GROUPS.items():
            for name in doc.get(section, {}) or {}:
                groups_by_name.setdefault(name, set()).add(group)
        return groups_by_name

    def _compute_member_ownership(self, root_data: dict[str, Any]) -> dict[tuple[str, str], set[str]]:
        """
        Build a `(name, version) -> set of workspace-member names` map.

        When `root_data` is the `npm list` output rooted at a workspace,
        each top-level entry whose `resolved` field starts with `file:`
        is a workspace member. Walk into each such subtree separately
        and attribute every reachable `(name, version)` pair to that
        member. A package that appears in two members' subtrees ends up
        with both names in its set, which is exactly what the override
        planner needs to spot a "shared transitive" worth pinning at
        the workspace root rather than per-member.

        Returns an empty dict when there's no workspace (no `file:`
        resolved top-level entries). The walk delegates to
        `_walk_member_subtree` so the static helper can be tested
        directly without standing up a full ecosystem instance.
        """
        ownership: dict[tuple[str, str], set[str]] = {}
        for member_name, info in (root_data.get("dependencies") or {}).items():
            resolved = str(info.get("resolved", ""))
            if not resolved.startswith("file:"):
                continue
            self._walk_member_subtree(info, member_name, ownership)
        return ownership

    def workspace_topology(self) -> WorkspaceTopology | None:
        """
        Detect an npm workspace by reading the lockfile-rooted `package.json`.

        Walks up to find the workspace root (the directory that owns the
        lockfile, which may be `self.root` itself or an ancestor for a
        member project). If the root's `package.json` declares a
        `workspaces` field, expand the globs against the root directory
        and read each member's `name` from its own `package.json`.

        Returns `None` when there's no lockfile, no `workspaces` field,
        or none of the globs resolve to a directory with a readable
        `package.json`.

        The work is split into `_locate_workspace_root`, which finds the
        directory that owns the lockfile and reads its `package.json`,
        and `_discover_workspace_members`, which expands the glob
        patterns and assembles the name -> directory map. Both helpers
        are static so they can be exercised independently of a live
        `NpmEcosystem` instance.
        """
        located = self._locate_workspace_root(self.root, self._find_lockfile())
        if located is None:
            return None
        workspace_root, root_doc = located

        ws_field = root_doc.get("workspaces")
        # npm accepts either ["pkgs/*"] or {"packages": ["pkgs/*"]}.
        if isinstance(ws_field, dict):
            patterns = ws_field.get("packages") or []
        elif isinstance(ws_field, list):
            patterns = ws_field
        else:
            return None
        if not patterns:
            return None

        members = self._discover_workspace_members(workspace_root, patterns)
        if not members:
            return None
        return WorkspaceTopology(root=workspace_root, members=members)

    def _find_workspace_member(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """
        Locate the workspace-member subtree in `npm list` output that matches `self.root`.

        When `npm list` runs inside a workspace member it walks up to the
        workspace root and reports the whole workspace tree. Each member shows
        up as a top-level entry keyed by its declared package name. Read the
        member's own `package.json` for that name and return the matching
        subtree.

        Returns `None` when `self.root` is itself the workspace root, when
        the member's `package.json` is unreadable or unnamed, or when no
        matching entry is present in the workspace tree.
        """
        lock_path = self._find_lockfile()
        if lock_path is None:
            return None
        workspace_root = lock_path.parent
        if workspace_root.resolve() == self.root.resolve():
            return None
        member_pkg_path = self.root / "package.json"
        if not member_pkg_path.is_file():
            return None
        try:
            member_doc = json.loads(member_pkg_path.read_text())
        except json.JSONDecodeError:
            return None
        member_name = member_doc.get("name")
        if not member_name:
            return None
        node = (data.get("dependencies") or {}).get(member_name)
        if node is None:
            return None
        return node

    def _find_lockfile(self) -> Path | None:
        """
        Locate a usable lockfile for transitive attribution.

        Looks in this order:

        1. `<root>/package-lock.json` -- the standard location.
        2. `<root>/node_modules/.package-lock.json` -- npm writes one of
           these whenever it installs, even when the project itself doesn't
           ship a lockfile (workspace members, for instance).
        3. The same two paths walking up the directory tree, so a workspace
           member can borrow its workspace root's lockfile.

        Returns the first existing path or `None`.
        """
        candidates = [
            self.root / "package-lock.json",
            self.root / "node_modules" / ".package-lock.json",
        ]
        for c in candidates:
            if c.is_file():
                return c
        cur = self.root.parent
        seen: set[Path] = set()
        while cur != cur.parent and cur not in seen:
            seen.add(cur)
            for tail in ("package-lock.json", "node_modules/.package-lock.json"):
                p = cur / tail
                if p.is_file():
                    return p
            cur = cur.parent
        return None

    def _npm_list(self, depth: int | None) -> dict[str, Any]:
        """
        Run `npm list --all --json` from `self.root` and return the parsed tree.

        npm's exit code is informational rather than fatal: a `1` exit means
        "missing or extraneous packages were detected" and is the normal
        outcome for any non-trivial project, so both `0` and `1` are
        accepted. Anything else (a crash, a permissions error) raises
        `EcosystemError` with the captured stderr.

        Returns `{}` when stdout is empty (npm reports nothing for projects
        with no dependencies at all). A non-empty body that doesn't parse as
        JSON raises `EcosystemError`; that's a bug in npm or a corrupted
        install rather than something to paper over.
        """
        cmd = ["npm", "list", "--all", "--json"]
        if depth is not None:
            cmd.append(f"--depth={depth}")
        result = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True)
        EcosystemError.require_condition(
            result.returncode in (0, 1),
            f"`npm list` failed with exit code {result.returncode}: {result.stderr.strip()}",
        )
        if not result.stdout.strip():
            return {}
        with EcosystemError.handle_errors(
            "`npm list` returned non-JSON output",
            handle_exc_class=json.JSONDecodeError,
        ):
            return json.loads(result.stdout)

    def _npm_list_at_workspace_root(self) -> dict[str, Any] | None:
        """
        Run `npm list --all --json` from the workspace root.

        Returns `None` when `self.root` is the workspace root itself
        (no extra call needed; the caller already has that data) or when
        no lockfile-owning ancestor exists.

        Failures here are non-fatal (returns `None`) because cross-member
        ownership attribution is an enrichment, not a correctness
        requirement; the rest of `load_installed` still produces a
        usable report without it.
        """
        lock_path = self._find_lockfile()
        if lock_path is None:
            return None
        workspace_root = lock_path.parent
        if workspace_root.resolve() == self.root.resolve():
            return None
        cmd = ["npm", "list", "--all", "--json"]
        result = subprocess.run(cmd, cwd=workspace_root, capture_output=True, text=True)
        if result.returncode not in (0, 1) or not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

    def range_satisfies(self, version: str, range_spec: str) -> bool:
        """
        Check whether `version` satisfies an npm semver `range_spec`.

        Shells out to `node -e "require('semver').satisfies(...)"`. If
        node or the semver package isn't available, conservatively
        returns `True` (the original script's "assume compatible"
        fallback for transitive deps with no discoverable range).
        """
        script = (
            f"const s=require('semver');process.exit(s.satisfies({json.dumps(version)},{json.dumps(range_spec)})?0:1)"
        )
        try:
            result = subprocess.run(
                ["node", "-e", script],
                capture_output=True,
                text=True,
                cwd=self.root,
            )
        except FileNotFoundError:
            logger.warning("node not found on PATH; assuming range is satisfied")
            return True
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        # Any other exit code (parse error, missing semver module) is
        # treated as an unknown answer; default to permissive to avoid
        # spurious rollbacks.
        logger.warning(f"node semver check failed for {version} against {range_spec!r}: {result.stderr.strip()}")
        return True

    def parse_version(self, version: str) -> ParsedVersion | None:
        """
        Parse a version string with strict semver semantics.

        npm publishes its registry data in semver form (`MAJOR.MINOR.PATCH`
        with optional `-prerelease` and `+build`), so anything that
        doesn't fit that grammar gets `None`. The cooldown engine treats
        `None` as "skip this candidate" rather than raising, so a
        non-semver oddity like a date-tagged version doesn't block the
        rest of the search.

        The returned `ParsedVersion` carries the original string verbatim
        so safe versions round-trip back through fix actions in the
        exact form the registry published.

        The sort key wraps the parsed `semver.Version` itself in a
        single-element tuple. `semver.Version` already compares the way
        npm expects (pre-releases sort before their final version, build
        metadata is ignored), so we don't need anything custom here.
        """
        try:
            v = semver.Version.parse(version)
        except ValueError:
            return None

        return ParsedVersion(
            original=version,
            major=v.major,
            minor=v.minor,
            micro=v.patch,
            is_prerelease=v.prerelease is not None,
            sort_key=(v,),
        )

    def apply_fixes(self, actions: list[FixAction]) -> AppliedFixes:
        """
        Apply pins. Routes `via_overrides` actions through `apply_override_fixes`.

        Splits the incoming actions into two groups based on the
        `via_overrides` flag, which the planner sets for shared
        transitive violations in workspace contexts. Direct pins land
        in `self.root`'s `package.json` `dependencies`; override pins
        go through the workspace-root override path. Both groups
        trigger their own `npm install`, in this order: write direct
        pins first, then `npm install` from the member, then write
        overrides at the workspace root, then `npm install` from there.

        When the override path returns `None` (the planner tagged
        `via_overrides` but no workspace root could be located), the
        action falls back to a direct pin so it isn't silently lost.
        That fallback is genuinely defensive: with the current planner
        and the two shipping ecosystems, the override path always
        resolves when the planner asked for it.
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
        Pin a list of direct-style actions into `self.root/package.json` and re-install.

        Each action becomes a single entry in `dependencies` formatted
        according to its `style` (exact pin or caret range). Existing
        entries with the same package name are overwritten in place;
        new packages are appended. The whole batch writes back in one
        shot, then `npm install` runs once to refresh the lockfile and
        actually move the bits into `node_modules`.

        A non-zero exit from `npm install` raises `EcosystemError` with
        the captured stderr; partial edits are left on disk in that
        case so the user can see what was attempted.

        The recorded `manifest_path` is always `"package.json"` (project
        relative) since direct pins only ever touch the project's own
        manifest.
        """
        result = AppliedFixes()
        root_pkg_path = self.root / "package.json"
        EcosystemError.require_condition(
            root_pkg_path.is_file(),
            f"No package.json at project root: {root_pkg_path}",
        )

        root_pkg = json.loads(root_pkg_path.read_text())
        deps = root_pkg.setdefault("dependencies", {})
        manifest_path = Path("package.json")

        for action in actions:
            spec = self._format_npm_spec(action.version, action.style)
            deps[action.package] = spec
            result.log.append(f"pinned {action.package} -> {spec}")
            result.entries.append(
                AppliedFix(
                    action=action,
                    pinned_spec=spec,
                    via_overrides=False,
                    manifest_path=manifest_path,
                )
            )

        root_pkg_path.write_text(json.dumps(root_pkg, indent=2) + "\n")

        proc = subprocess.run(["npm", "install"], cwd=self.root, capture_output=True, text=True)
        EcosystemError.require_condition(
            proc.returncode == 0,
            f"`npm install` failed after applying fixes: {proc.stderr.strip()}",
        )
        result.log.append("ran: npm install")
        return result

    def remove_managed_pin(self, pin: ManagedPin) -> RemovalOutcome:
        """
        Reverse a previously-applied managed pin from the project's `package.json`.

        For `PinMechanism.DIRECT` this removes the entry from
        `dependencies` (and the parallel `devDependencies`,
        `optionalDependencies`, `peerDependencies` blocks if the pin
        landed there). For `PinMechanism.OVERRIDE` this removes the
        entry from `overrides` at the recorded manifest path.

        See `Ecosystem.remove_managed_pin` for outcome semantics.
        """
        path = self.root / pin.manifest_path
        if not path.is_file():
            return RemovalOutcome.ORPHAN

        pkg = json.loads(path.read_text())
        if pin.mechanism is PinMechanism.OVERRIDE:
            outcome = self._remove_npm_override_entry(pkg, pin)
        else:
            outcome = self._remove_npm_direct_entry(pkg, pin)
        if outcome is RemovalOutcome.REMOVED:
            path.write_text(json.dumps(pkg, indent=2) + "\n")
        return outcome

    def regenerate_lockfile(self) -> str:
        """Recompute `package-lock.json` by running `npm install` from the project root."""
        proc = subprocess.run(["npm", "install"], cwd=self.root, capture_output=True, text=True)
        EcosystemError.require_condition(
            proc.returncode == 0,
            f"`npm install` failed during lockfile regeneration: {proc.stderr.strip()}",
        )
        return "ran: npm install"

    def supports_overrides(self) -> bool:
        return True

    def apply_override_fixes(self, actions: list[FixAction]) -> AppliedFixes | None:
        """
        Force transitive versions via npm's `overrides` field.

        Direct pins in `dependencies` only affect what the project's own
        code resolves to. When a violating version is hoisted at the
        workspace-root `node_modules` (where a different consumer in the
        tree pulled it in), a direct pin in a workspace-member's
        `package.json` can leave that root copy untouched. `overrides`
        is npm's blessed mechanism for forcing one resolution everywhere
        regardless of who declared it.

        Overrides must live in the workspace root's `package.json` to
        apply tree-wide, so this writes to the directory that owns the
        lockfile rather than `self.root` (which may be a workspace
        member). When that root manifest doesn't exist, return `None`
        so the caller can fall back to direct pinning.
        """
        if not actions:
            return AppliedFixes()
        lock_path = self._find_lockfile()
        workspace_root = lock_path.parent if lock_path is not None else self.root
        root_pkg_path = workspace_root / "package.json"
        if not root_pkg_path.is_file():
            return None
        # When workspace_root sits above self.root, `root_pkg_path` isn't
        # relative to the project root and we record the absolute path
        # instead so cleanup can still find it.
        if root_pkg_path.is_relative_to(self.root):
            manifest_path = root_pkg_path.relative_to(self.root)
        else:
            manifest_path = root_pkg_path

        result = AppliedFixes()
        root_pkg = json.loads(root_pkg_path.read_text())
        overrides = root_pkg.setdefault("overrides", {})
        for action in actions:
            overrides[action.package] = action.version
            result.log.append(f"overrode {action.package} -> {action.version} (workspace root)")
            result.entries.append(
                AppliedFix(
                    action=action,
                    pinned_spec=action.version,
                    via_overrides=True,
                    manifest_path=manifest_path,
                )
            )
        root_pkg_path.write_text(json.dumps(root_pkg, indent=2) + "\n")

        proc = subprocess.run(["npm", "install"], cwd=workspace_root, capture_output=True, text=True)
        EcosystemError.require_condition(
            proc.returncode == 0,
            f"`npm install` failed after applying overrides: {proc.stderr.strip()}",
        )
        result.log.append(f"ran: npm install (in {workspace_root})")
        return result

    @staticmethod
    def _walk_member_subtree(
        node: dict[str, Any],
        owner: str,
        ownership: dict[tuple[str, str], set[str]],
    ) -> None:
        """
        Walk one workspace member's subtree in `npm list` output and tag every
        reachable `(name, version)` pair with `owner`.

        Mutation-in-place is deliberate: `ownership` is the caller's
        accumulator across every member, and threading it back through a
        return value would force the caller to merge dicts. Returning
        `None` keeps the call shape symmetric across the per-member loop
        in `_compute_member_ownership`.

        Entries without a resolved `version` are skipped silently; they
        appear in `npm list` output for unresolved peer deps and similar
        dangling references that don't actually contribute to the
        installed set.
        """
        for name, child in (node.get("dependencies") or {}).items():
            version = child.get("version")
            if not version:
                continue
            ownership.setdefault((name, version), set()).add(owner)
            NpmEcosystem._walk_member_subtree(child, owner, ownership)

    @staticmethod
    def _attribute_groups_to_installs(
        data: dict[str, Any],
        groups_by_name: dict[str, set[DependencyGroup]],
    ) -> dict[tuple[str, str], set[DependencyGroup]]:
        """
        Walk the `npm list` tree once per top-level entry to attribute each
        reachable `(name, version)` to its principal's groups.

        For each top-level dependency in `data`, look up the declared
        groups in `groups_by_name`; if none are recorded the entry gets
        `MAIN` as a conservative default so it isn't filtered out
        unexpectedly. Then walk the entire subtree under that top-level
        and union the principal's groups into every reachable install's
        accumulated set.

        Transitives reached through multiple top-levels accumulate the
        union of their groups, matching the runner's "included if
        reachable through any included group" semantic.

        Returns a mapping from `(name, version)` to the set of groups
        that any path through the tree attributed to that install.
        Packages with no resolved `version` field are skipped silently
        (same dangling-reference filter as `_walk_member_subtree`).
        """
        groups_by_install: dict[tuple[str, str], set[DependencyGroup]] = {}
        for top_name, top_info in (data.get("dependencies") or {}).items():
            top_version = top_info.get("version")
            if not top_version:
                continue
            top_groups = groups_by_name.get(top_name, {DependencyGroup.MAIN})
            for group in top_groups:
                NpmEcosystem._tag_subtree_with_group(
                    {"dependencies": {top_name: top_info}},
                    group,
                    groups_by_install,
                )
        return groups_by_install

    @staticmethod
    def _tag_subtree_with_group(
        node: dict[str, Any],
        group: DependencyGroup,
        groups_by_install: dict[tuple[str, str], set[DependencyGroup]],
    ) -> None:
        """
        Recursively tag every `(name, version)` reachable from `node` with `group`.

        Mutation-in-place follows the same pattern as
        `_walk_member_subtree`: the caller threads a single accumulator
        through every per-group walk and merges happen for free in the
        shared map. Entries missing a resolved `version` are skipped.
        """
        for name, info in (node.get("dependencies") or {}).items():
            version = info.get("version")
            if not version:
                continue
            groups_by_install.setdefault((name, version), set()).add(group)
            NpmEcosystem._tag_subtree_with_group(info, group, groups_by_install)

    @staticmethod
    def _collect_installed_packages(
        *,
        data: dict[str, Any],
        ecosystem: EcosystemKind,
        ownership: dict[tuple[str, str], set[str]],
        groups_by_install: dict[tuple[str, str], set[DependencyGroup]],
    ) -> list[InstalledPackage]:
        """
        Walk the `npm list` tree to build the final `InstalledPackage` records.

        For each `(name, version)` first-seen during a depth-first walk
        of the dependency tree, build one `InstalledPackage` carrying:

        - The name, version, and ecosystem identity.
        - The `via_chain`: empty for top-level entries (principals),
          otherwise the ancestor path from the immediate parent up to
          the principal. The chain is built bottom-up while walking
          (parent name appended at each descent) and reversed so the
          immediate parent comes first.
        - The deterministic, alphabetically-sorted tuple of workspace
          member names that own this install (from `ownership`).
        - The deterministic, alphabetically-sorted-by-`group.value`
          tuple of dependency groups attributed to this install (from
          `groups_by_install`).

        First-seen wins because npm dedupes its tree by hoisting; the
        same `(name, version)` may appear in multiple subtrees but its
        `InstalledPackage` is one record. Subsequent visits still
        descend into their children so transitives reached only through
        the deeper occurrences are collected too.
        """
        packages: dict[tuple[str, str], InstalledPackage] = {}
        NpmEcosystem._collect_walk(
            node=data,
            chain=(),
            ecosystem=ecosystem,
            ownership=ownership,
            groups_by_install=groups_by_install,
            packages=packages,
        )
        return list(packages.values())

    @staticmethod
    def _collect_walk(
        *,
        node: dict[str, Any],
        chain: tuple[str, ...],
        ecosystem: EcosystemKind,
        ownership: dict[tuple[str, str], set[str]],
        groups_by_install: dict[tuple[str, str], set[DependencyGroup]],
        packages: dict[tuple[str, str], InstalledPackage],
    ) -> None:
        """
        Inner DFS body shared by `_collect_installed_packages`.

        Kept separate from the outer entry point so the recursive call
        site reads cleanly and so the caller doesn't have to construct
        a meaningful starting `chain` (always `()` at the root).
        """
        for name, info in (node.get("dependencies") or {}).items():
            version = info.get("version")
            if not version:
                continue
            key = (name, version)
            if key not in packages:
                via_chain: tuple[str, ...] = tuple(reversed(chain))
                owners = tuple(sorted(ownership.get(key, set())))
                install_groups = tuple(sorted(groups_by_install.get(key, set()), key=lambda g: g.value))
                packages[key] = InstalledPackage(
                    name=name,
                    version=version,
                    ecosystem=ecosystem,
                    via_chain=via_chain,
                    member_owners=owners,
                    groups=install_groups,
                )
            NpmEcosystem._collect_walk(
                node=info,
                chain=chain + (name,),
                ecosystem=ecosystem,
                ownership=ownership,
                groups_by_install=groups_by_install,
                packages=packages,
            )

    @staticmethod
    def _locate_workspace_root(
        start: Path,
        lock_path: Path | None,
    ) -> tuple[Path, dict[str, Any]] | None:
        """
        Identify the workspace root directory and load its `package.json`.

        The workspace root is the directory that owns the lockfile, with
        `start` itself as the fallback when no lockfile is present. That
        directory's `package.json` is the manifest that declares the
        workspace layout (the `workspaces` field that
        `workspace_topology` consumes).

        Returns `(workspace_root, parsed_doc)` on success, or `None`
        when:

        - The root `package.json` is missing entirely.
        - The root `package.json` exists but doesn't parse as JSON.

        Both conditions are non-fatal: workspace topology is an
        enrichment, and a corrupt or missing root manifest just means we
        skip the workspace optimization rather than failing the whole
        check.
        """
        workspace_root = lock_path.parent if lock_path is not None else start
        root_pkg_path = workspace_root / "package.json"
        if not root_pkg_path.is_file():
            return None
        try:
            root_doc = json.loads(root_pkg_path.read_text())
        except json.JSONDecodeError:
            return None
        return (workspace_root, root_doc)

    @staticmethod
    def _discover_workspace_members(
        workspace_root: Path,
        patterns: list[str],
    ) -> dict[str, Path]:
        """
        Resolve `workspaces` glob patterns to a name -> directory map.

        Each pattern is expanded relative to `workspace_root`, then every
        match is filtered through several gates before becoming a
        workspace member:

        - Must be a directory. `glob.glob` returns files too; npm
          workspaces only mean directories with a `package.json`.
        - Must contain a readable `package.json`. Member candidates
          without one are silently skipped; npm treats those as not-yet
          initialized directories rather than errors.
        - The member's `package.json` must declare `name`. Without a
          name there's no key to slot the member under in the topology.

        Members that pass all the gates are keyed in the result by their
        declared `name`. Returns an empty mapping when no patterns
        produced a usable member; the caller turns that into a `None`
        topology so the runner skips workspace-aware behavior.
        """
        members: dict[str, Path] = {}
        for pattern in patterns:
            for match in glob.glob(str(workspace_root / pattern)):
                member_dir = Path(match)
                if not member_dir.is_dir():
                    continue
                member_pkg = member_dir / "package.json"
                if not member_pkg.is_file():
                    continue
                try:
                    member_doc = json.loads(member_pkg.read_text())
                except json.JSONDecodeError:
                    continue
                name = member_doc.get("name")
                if not name:
                    continue
                members[name] = member_dir
        return members

    @staticmethod
    def _remove_npm_direct_entry(pkg: dict, pin: ManagedPin) -> RemovalOutcome:
        """
        Find and remove a direct-dependency entry for `pin.package` from `pkg`.

        Walks every dependency block npm recognizes (`dependencies`,
        `devDependencies`, `optionalDependencies`, `peerDependencies`)
        and looks up the package by name. Returns `REMOVED` if the
        value matches `pin.pinned_spec`, `DRIFTED` if the entry exists
        with a different value, or `ORPHAN` if no matching entry is
        found anywhere.
        """
        for block_name in NPM_SECTION_GROUPS:
            block = pkg.get(block_name)
            if not isinstance(block, dict) or pin.package not in block:
                continue
            if block[pin.package] == pin.pinned_spec:
                del block[pin.package]
                return RemovalOutcome.REMOVED
            return RemovalOutcome.DRIFTED
        return RemovalOutcome.ORPHAN

    @staticmethod
    def _remove_npm_override_entry(pkg: dict, pin: ManagedPin) -> RemovalOutcome:
        """
        Find and remove an override entry for `pin.package` from `pkg["overrides"]`.

        Returns `REMOVED` if the override value matches
        `pin.pinned_spec`, `DRIFTED` if a same-named entry exists with
        a different value, or `ORPHAN` if no matching entry is found.
        """
        overrides = pkg.get("overrides")
        if not isinstance(overrides, dict) or pin.package not in overrides:
            return RemovalOutcome.ORPHAN
        if overrides[pin.package] == pin.pinned_spec:
            del overrides[pin.package]
            return RemovalOutcome.REMOVED
        return RemovalOutcome.DRIFTED

    @staticmethod
    def _format_npm_spec(version: str, style: FixStyle) -> str:
        """
        Render the new dependency value for an npm pin.

        For `FixStyle.EXACT` the result is the bare version string
        (`X.Y.Z`), which npm treats as an exact pin.

        For `FixStyle.COMPATIBLE` the result is the caret form
        (`^X.Y.Z`), which npm interprets as "any release that doesn't
        change the leftmost non-zero component". For non-prerelease
        versions with a nonzero major this is equivalent to
        `>={version},<{M+1}.0.0`, the same shape pypi's compatible
        style produces.
        """
        if style is FixStyle.EXACT:
            return version
        return f"^{version}"

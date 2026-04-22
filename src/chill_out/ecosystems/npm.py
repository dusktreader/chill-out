"""
npm ecosystem backend.

Reads installed packages from ``npm list --json`` and from ``package-lock.json``
for transitive resolution. Talks to the npm registry. Applies fixes by editing
the root ``package.json`` to pin every safe version (direct or promoted-from-
transitive) into ``dependencies``, then re-running ``npm install``.
"""

from __future__ import annotations

import glob
import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pendulum
from loguru import logger

from chill_out.constants import DependencyGroup, EcosystemKind, FixStyle
from chill_out.ecosystems.base import Ecosystem, RegistryClient
from chill_out.exceptions import EcosystemError, RegistryError
from chill_out.models import (
    FixAction,
    InstalledPackage,
    PackageInfo,
    PackageRelease,
    VersionManifest,
    WorkspaceTopology,
)

NPM_REGISTRY = "https://registry.npmjs.org"

# Maps each ``package.json`` dependency section to its semantic group.
# Used in both directions: direct attribution from the manifest, and
# transitive inheritance when walking the npm-list tree per top-level
# group.
NPM_SECTION_GROUPS: dict[str, DependencyGroup] = {
    "dependencies": DependencyGroup.MAIN,
    "devDependencies": DependencyGroup.DEV,
    "optionalDependencies": DependencyGroup.OPTIONAL,
    "peerDependencies": DependencyGroup.PEER,
}


class NpmRegistryClient(RegistryClient):
    """Async client for the public npm registry."""

    base_url: str = NPM_REGISTRY

    async def fetch_package(self, name: str) -> PackageInfo | None:
        """
        Fetch all release timestamps for a package.

        Returns ``None`` if the package is missing (404) or if the body is unusable.
        Raises :class:`RegistryError` on transport failures.
        """
        url = f"{self.base_url}/{name}"
        try:
            res = await self.http.get(url)
        except httpx.TransportError as exc:
            raise RegistryError(f"npm registry transport error for {name}: {exc}") from exc
        if res.status_code == 404:
            return None
        if res.status_code != 200:
            raise RegistryError(f"npm registry returned HTTP {res.status_code} for {name}")
        try:
            data = res.json()
        except json.JSONDecodeError as exc:
            raise RegistryError(f"npm registry returned non-JSON body for {name}: {exc}") from exc
        time_map: dict[str, str] = data.get("time", {}) or {}
        releases: dict[str, PackageRelease] = {}
        for ver, iso in time_map.items():
            if ver in {"created", "modified"}:
                continue
            try:
                published = pendulum.parse(iso)
            except (ValueError, TypeError):
                continue
            assert isinstance(published, pendulum.DateTime)
            releases[ver] = PackageRelease(version=ver, published=published)
        return PackageInfo(name=name, releases=releases)

    async def fetch_version_manifest(self, name: str, version: str) -> VersionManifest | None:
        """Fetch dependency declarations for ``{name}@{version}`` from the npm registry."""
        url = f"{self.base_url}/{name}/{version}"
        try:
            res = await self.http.get(url)
        except httpx.TransportError as exc:
            raise RegistryError(f"npm registry transport error for {name}@{version}: {exc}") from exc
        if res.status_code == 404:
            return None
        if res.status_code != 200:
            raise RegistryError(f"npm registry returned HTTP {res.status_code} for {name}@{version}")
        try:
            data = res.json()
        except json.JSONDecodeError as exc:
            raise RegistryError(f"npm registry returned non-JSON body for {name}@{version}: {exc}") from exc
        deps: dict[str, str] = {}
        deps.update(data.get("dependencies") or {})
        deps.update(data.get("peerDependencies") or {})
        return VersionManifest(name=name, version=version, deps=deps)


class NpmEcosystem(Ecosystem):
    """Ecosystem backend for npm projects."""

    kind: EcosystemKind = EcosystemKind.NPM

    @classmethod
    def detect(cls, root: Path) -> bool:
        return (root / "package.json").is_file()

    def make_client(self, http: httpx.AsyncClient) -> RegistryClient:
        return NpmRegistryClient(http)

    # ------------------------------------------------------------------
    # Installed package enumeration
    # ------------------------------------------------------------------

    def load_installed(self, *, deep: bool = False) -> list[InstalledPackage]:
        if deep:
            return self._load_all()
        return self._load_direct()

    def _read_root_package_json(self) -> dict[str, set[DependencyGroup]]:
        """
        Read the root ``package.json`` and return a per-dependency group map.

        The returned mapping is keyed by package name; each value is the set
        of semantic groups the package is declared in. A single package can
        appear in multiple sections (e.g. both ``dependencies`` and
        ``peerDependencies``) and gets every matching group.

        Workspaces and nested ``package.json`` files are intentionally ignored;
        for monorepos, run chill-out from each sub-project's directory.
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

    def _load_direct(self) -> list[InstalledPackage]:
        groups_by_name = self._read_root_package_json()
        # Use the full tree because workspace members appear as `file:`-resolved
        # top-level entries; their actual installed deps are nested one level
        # deeper. Descending into those nodes lets a workspace member find its
        # own declared deps even when npm-list is rooted at the workspace.
        data = self._npm_list(depth=None)

        packages: dict[str, InstalledPackage] = {}

        def collect(node: dict[str, Any], descend_workspace: bool) -> None:
            for name, info in (node.get("dependencies") or {}).items():
                resolved = str(info.get("resolved", ""))
                is_workspace_member = resolved.startswith("file:")
                if is_workspace_member:
                    # Don't report the workspace member itself; descend into
                    # its deps to find the ones declared by THIS project.
                    if descend_workspace:
                        collect(info, descend_workspace=False)
                    continue
                if name not in groups_by_name:
                    continue
                version = info.get("version")
                if not version:
                    continue
                if name not in packages:
                    packages[name] = InstalledPackage(
                        name=name,
                        version=version,
                        ecosystem=self.kind,
                        groups=tuple(sorted(groups_by_name[name], key=lambda g: g.value)),
                    )

        collect(data, descend_workspace=True)

        return list(packages.values())

    def _load_all(self) -> list[InstalledPackage]:
        data = self._npm_list(depth=None)

        # Compute cross-member ownership by running npm list at the workspace
        # root. When self.root is a workspace member, npm scopes its own
        # output to that member's subtree, which would misattribute every
        # install to a single owner. Walking the lockfile-rooted tree gives
        # us the full picture: each top-level file:-resolved entry is a
        # workspace member and its subtree shows everything that member
        # pulls in. In a non-workspace context this falls back to the
        # already-loaded data and the index ends up empty.
        ownership_data = self._npm_list_at_workspace_root() or data
        ownership = self._compute_member_ownership(ownership_data)

        # If we're inside a workspace member, npm list walks up to the
        # workspace root and reports every member's tree. Scope down to just
        # this member's subtree so we don't surface (and try to fix) packages
        # that belong to a sibling member.
        member_node = self._find_workspace_member(data)
        if member_node is not None:
            data = member_node

        # Group attribution for the deep walk: read this project's own
        # package.json to find which top-level names belong to which
        # semantic group, then walk the npm-list tree once per top-level
        # entry and tag every reachable (name, version) with the group of
        # that top-level. Transitives reached through multiple top-levels
        # accumulate the union of their groups, matching the runner's
        # "included if reachable through any included group" semantic.
        groups_by_name = self._read_root_package_json()
        groups_by_install: dict[tuple[str, str], set[DependencyGroup]] = {}

        def attribute(node: dict[str, Any], group: DependencyGroup) -> None:
            for name, info in (node.get("dependencies") or {}).items():
                version = info.get("version")
                if not version:
                    continue
                groups_by_install.setdefault((name, version), set()).add(group)
                attribute(info, group)

        for top_name, top_info in (data.get("dependencies") or {}).items():
            top_version = top_info.get("version")
            if not top_version:
                continue
            # Top-level entries declared in package.json get their declared
            # groups; anything not declared at the root (orphaned installs,
            # for instance) gets MAIN as a conservative default so it isn't
            # filtered out unexpectedly.
            top_groups = groups_by_name.get(top_name, {DependencyGroup.MAIN})
            for g in top_groups:
                attribute({"dependencies": {top_name: top_info}}, g)

        # Dedupe by (name, version), not by name. npm routinely installs the
        # same package at multiple distinct versions -- one hoisted to the
        # shallowest node_modules, others nested under specific parents --
        # and each copy actually loads at runtime for whichever code requires
        # it. Treat them as separate installations so cooldown checks see all
        # of them. The npm-list tree gives us the exact path that pulled in
        # each copy, so via_chain is read straight from the walk position
        # rather than reconstructed from the lockfile graph.
        packages: dict[tuple[str, str], InstalledPackage] = {}

        def collect(node: dict[str, Any], chain: tuple[str, ...]) -> None:
            for name, info in (node.get("dependencies") or {}).items():
                version = info.get("version")
                if not version:
                    continue
                key = (name, version)
                if key not in packages:
                    # Empty chain means this is a top-level installation --
                    # report it as a principal. Anything deeper gets the
                    # ancestor path (immediate parent first, principal last)
                    # as its via_chain.
                    via_chain: tuple[str, ...] = tuple(reversed(chain))
                    owners = tuple(sorted(ownership.get(key, set())))
                    install_groups = tuple(
                        sorted(groups_by_install.get(key, set()), key=lambda g: g.value)
                    )
                    packages[key] = InstalledPackage(
                        name=name,
                        version=version,
                        ecosystem=self.kind,
                        via_chain=via_chain,
                        member_owners=owners,
                        groups=install_groups,
                    )
                collect(info, chain + (name,))

        collect(data, ())

        return list(packages.values())

    def _compute_member_ownership(self, root_data: dict[str, Any]) -> dict[tuple[str, str], set[str]]:
        """
        Build a (name, version) -> set of workspace-member names that pull it in.

        When ``root_data`` is the npm-list output rooted at a workspace, each
        top-level entry whose ``resolved`` field starts with ``file:`` is a
        workspace member. Walk into each such subtree separately and attribute
        every reachable (name, version) pair to that member. A package that
        appears in two members' subtrees ends up with both names in its set.

        Returns an empty dict when there's no workspace (no ``file:``-resolved
        top-level entries).
        """
        ownership: dict[tuple[str, str], set[str]] = {}
        for member_name, info in (root_data.get("dependencies") or {}).items():
            resolved = str(info.get("resolved", ""))
            if not resolved.startswith("file:"):
                continue

            def walk(node: dict[str, Any], owner: str) -> None:
                for name, child in (node.get("dependencies") or {}).items():
                    version = child.get("version")
                    if not version:
                        continue
                    ownership.setdefault((name, version), set()).add(owner)
                    walk(child, owner)

            walk(info, member_name)
        return ownership

    def workspace_topology(self) -> WorkspaceTopology | None:
        """
        Detect an npm workspace by reading the lockfile-rooted ``package.json``.

        Walks up to find the workspace root (the directory that owns the
        lockfile, which may be ``self.root`` itself or an ancestor for a
        member project). If the root's ``package.json`` declares a
        ``workspaces`` field, expand the globs against the root directory
        and read each member's ``name`` from its own ``package.json``.

        Returns ``None`` when there's no lockfile, no ``workspaces`` field,
        or none of the globs resolve to a directory with a readable
        ``package.json``.
        """
        lock_path = self._find_lockfile()
        workspace_root = lock_path.parent if lock_path is not None else self.root
        root_pkg_path = workspace_root / "package.json"
        if not root_pkg_path.is_file():
            return None
        try:
            root_doc = json.loads(root_pkg_path.read_text())
        except json.JSONDecodeError:
            return None
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
        if not members:
            return None
        return WorkspaceTopology(root=workspace_root, members=members)

    def _find_workspace_member(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """
        Locate the workspace-member subtree in ``npm list`` output that matches ``self.root``.

        When ``npm list`` runs inside a workspace member it walks up to the
        workspace root and reports the whole workspace tree. Each member shows
        up as a top-level entry keyed by its declared package name. Read the
        member's own ``package.json`` for that name and return the matching
        subtree.

        Returns ``None`` when ``self.root`` is itself the workspace root, when
        the member's ``package.json`` is unreadable or unnamed, or when no
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

        1. ``<root>/package-lock.json`` — the standard location.
        2. ``<root>/node_modules/.package-lock.json`` — npm writes one of
           these whenever it installs, even when the project itself doesn't
           ship a lockfile (workspace members, for instance).
        3. The same two paths walking up the directory tree, so a workspace
           member can borrow its workspace root's lockfile.

        Returns the first existing path or ``None``.
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
        cmd = ["npm", "list", "--all", "--json"]
        if depth is not None:
            cmd.append(f"--depth={depth}")
        result = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True)
        # npm list exits 1 when there are missing/extraneous packages — that's normal.
        EcosystemError.require_condition(
            result.returncode in (0, 1),
            f"`npm list` failed with exit code {result.returncode}: {result.stderr.strip()}",
        )
        if not result.stdout.strip():
            return {}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise EcosystemError(f"`npm list` returned non-JSON output: {exc}") from exc

    def _npm_list_at_workspace_root(self) -> dict[str, Any] | None:
        """
        Run ``npm list --all --json`` from the workspace root.

        Returns ``None`` when ``self.root`` is the workspace root itself
        (no extra call needed; the caller already has that data) or when
        no lockfile-owning ancestor exists.
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

    # ------------------------------------------------------------------
    # Fix application
    # ------------------------------------------------------------------

    def range_satisfies(self, version: str, range_spec: str) -> bool:
        """
        Check whether ``version`` satisfies an npm semver ``range_spec``.

        Shells out to ``node -e "require('semver').satisfies(...)"``. If node or
        the semver package isn't available, conservatively returns ``True`` (the
        original script's "assume compatible" fallback for transitive deps with
        no discoverable range).
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
        # Any other exit code (parse error, missing semver module) is treated as
        # an unknown answer; default to permissive to avoid spurious rollbacks.
        logger.warning(f"node semver check failed for {version} against {range_spec!r}: {result.stderr.strip()}")
        return True

    def apply_fixes(self, actions: list[FixAction]) -> list[str]:
        """Apply pins. Routes ``via_overrides`` actions through ``apply_override_fixes``.

        Splits the incoming actions into two groups based on the
        ``via_overrides`` flag, which the planner sets for shared
        transitive violations in workspace contexts. Direct pins land in
        ``self.root``'s ``package.json`` ``dependencies`` as before;
        override pins go through the workspace-root override path. Both
        groups trigger their own ``npm install`` so the ordering is:
        write direct pins, ``npm install`` from member, then write
        overrides at the workspace root, ``npm install`` from there.
        """
        if not actions:
            return []
        direct_actions = [a for a in actions if not a.via_overrides]
        override_actions = [a for a in actions if a.via_overrides]

        log: list[str] = []
        if direct_actions:
            log.extend(self._apply_direct_fixes(direct_actions))
        if override_actions:
            override_log = self.apply_override_fixes(override_actions)
            if override_log is None:
                # Workspace root not present (shouldn't happen if planner
                # tagged via_overrides, but stay defensive). Fall back to
                # direct pinning so the action isn't lost silently.
                logger.warning("override path unavailable; falling back to direct pins for shared actions")
                log.extend(self._apply_direct_fixes(override_actions))
            else:
                log.extend(override_log)
        return log

    def _apply_direct_fixes(self, actions: list[FixAction]) -> list[str]:
        """Pin a list of direct-style actions into ``self.root/package.json``."""
        log: list[str] = []
        root_pkg_path = self.root / "package.json"
        EcosystemError.require_condition(root_pkg_path.is_file(), f"No package.json at project root: {root_pkg_path}")

        root_pkg = json.loads(root_pkg_path.read_text())
        deps = root_pkg.setdefault("dependencies", {})

        for action in actions:
            spec = _format_npm_spec(action.version, action.style)
            deps[action.package] = spec
            log.append(f"pinned {action.package} -> {spec}")

        root_pkg_path.write_text(json.dumps(root_pkg, indent=2) + "\n")

        result = subprocess.run(["npm", "install"], cwd=self.root, capture_output=True, text=True)
        if result.returncode != 0:
            raise EcosystemError(f"`npm install` failed after applying fixes: {result.stderr.strip()}")
        log.append("ran: npm install")
        return log

    def supports_overrides(self) -> bool:
        return True

    def apply_override_fixes(self, actions: list[FixAction]) -> list[str] | None:
        """
        Force transitive versions via npm's ``overrides`` field.

        Direct pins in ``dependencies`` only affect what the project's own
        code resolves to. When a violating version is hoisted at the
        workspace-root ``node_modules`` (where a different consumer in the
        tree pulled it in), a direct pin in a workspace-member's
        ``package.json`` can leave that root copy untouched. ``overrides``
        is npm's blessed mechanism for forcing one resolution everywhere
        regardless of who declared it.

        Overrides must live in the workspace root's ``package.json`` to
        apply tree-wide, so this writes to the directory that owns the
        lockfile rather than ``self.root`` (which may be a workspace
        member).
        """
        if not actions:
            return []
        lock_path = self._find_lockfile()
        workspace_root = lock_path.parent if lock_path is not None else self.root
        root_pkg_path = workspace_root / "package.json"
        if not root_pkg_path.is_file():
            return None

        log: list[str] = []
        root_pkg = json.loads(root_pkg_path.read_text())
        overrides = root_pkg.setdefault("overrides", {})
        for action in actions:
            overrides[action.package] = action.version
            log.append(f"overrode {action.package} -> {action.version} (workspace root)")
        root_pkg_path.write_text(json.dumps(root_pkg, indent=2) + "\n")

        result = subprocess.run(["npm", "install"], cwd=workspace_root, capture_output=True, text=True)
        if result.returncode != 0:
            raise EcosystemError(f"`npm install` failed after applying overrides: {result.stderr.strip()}")
        log.append(f"ran: npm install (in {workspace_root})")
        return log


def _format_npm_spec(version: str, style: FixStyle) -> str:
    """Render the new dependency value for an npm pin.

    For :attr:`FixStyle.EXACT` the result is the bare version string
    (``X.Y.Z``), which npm treats as an exact pin.

    For :attr:`FixStyle.COMPATIBLE` the result is the caret form
    (``^X.Y.Z``), which npm interprets as "any release that doesn't change
    the leftmost non-zero component". For non-prerelease versions with a
    nonzero major this is equivalent to ``>={version},<{M+1}.0.0``, the
    same shape pypi's compatible style produces.
    """
    if style is FixStyle.EXACT:
        return version
    return f"^{version}"

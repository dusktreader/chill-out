"""
npm ecosystem backend.

Reads installed packages from ``npm list --json`` and from ``package-lock.json``
for transitive resolution. Talks to the npm registry. Applies fixes by editing
the root ``package.json`` to pin every safe version (direct or promoted-from-
transitive) into ``dependencies``, then re-running ``npm install``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pendulum
from loguru import logger

from chill_out.constants import EcosystemKind
from chill_out.ecosystems.base import Ecosystem, RegistryClient
from chill_out.exceptions import EcosystemError, RegistryError
from chill_out.models import FixAction, InstalledPackage, PackageInfo, PackageRelease, VersionManifest

NPM_REGISTRY = "https://registry.npmjs.org"


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

    def _read_root_package_json(self) -> set[str]:
        """
        Read the root ``package.json`` and return the set of declared dependency names.

        Workspaces and nested ``package.json`` files are intentionally ignored;
        for monorepos, run chill-out from each sub-project's directory.
        """
        dep_names: set[str] = set()
        root_pkg = self.root / "package.json"
        if not root_pkg.is_file():
            return dep_names
        try:
            doc = json.loads(root_pkg.read_text())
        except json.JSONDecodeError:
            logger.warning(f"Skipping unreadable package.json: {root_pkg}")
            return dep_names
        for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            for name in doc.get(section, {}) or {}:
                dep_names.add(name)
        return dep_names

    def _load_direct(self) -> list[InstalledPackage]:
        dep_names = self._read_root_package_json()
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
                if name not in dep_names:
                    continue
                version = info.get("version")
                if not version:
                    continue
                if name not in packages:
                    packages[name] = InstalledPackage(
                        name=name,
                        version=version,
                        ecosystem=self.kind,
                    )

        collect(data, descend_workspace=True)

        return list(packages.values())

    def _load_all(self) -> list[InstalledPackage]:
        dep_names = self._read_root_package_json()
        data = self._npm_list(depth=None)

        # If we're inside a workspace member, npm list walks up to the
        # workspace root and reports every member's tree. Scope down to just
        # this member's subtree so we don't surface (and try to fix) packages
        # that belong to a sibling member.
        member_node = self._find_workspace_member(data)
        if member_node is not None:
            data = member_node

        # Build a reverse-dep graph from package-lock.json so we can attribute
        # each transitive dep to a principal.
        required_by = self._build_required_by()

        def find_via_chain(name: str) -> tuple[str, ...]:
            visited = {name}
            prev: dict[str, str] = {}
            queue = [name]
            while queue:
                node = queue.pop(0)
                if node in dep_names and node != name:
                    path: list[str] = []
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

        packages: dict[str, InstalledPackage] = {}

        def collect(node: dict[str, Any]) -> None:
            # Two-pass per node: register every direct child first, then
            # recurse. Top-level wins over deeply nested copies — important
            # because npm hoists the shallowest version to ``node_modules/<pkg>``
            # and that's the one that actually gets loaded at runtime. A naive
            # depth-first walk would let a transitive duplicate shadow it.
            children = (node.get("dependencies") or {}).items()
            for name, info in children:
                version = info.get("version")
                if not version:
                    continue
                if name not in packages:
                    via_chain = () if name in dep_names else find_via_chain(name)
                    packages[name] = InstalledPackage(
                        name=name,
                        version=version,
                        ecosystem=self.kind,
                        via_chain=via_chain,
                    )
            for _, info in children:
                collect(info)

        collect(data)

        return list(packages.values())

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

    def _build_required_by(self) -> dict[str, set[str]]:
        lock_path = self._find_lockfile()
        if lock_path is None:
            logger.warning(
                f"No package-lock.json found at or above {self.root}; "
                "transitive dependency attribution will be skipped."
            )
            return {}
        try:
            lock = json.loads(lock_path.read_text())
        except json.JSONDecodeError:
            logger.warning(f"Skipping unreadable lockfile: {lock_path}")
            return {}
        required_by: dict[str, set[str]] = {}
        for path, info in (lock.get("packages") or {}).items():
            # Lockfile entries have keys like "node_modules/foo" or
            # "node_modules/foo/node_modules/bar"; the parent name we want is
            # the segment after the LAST "node_modules/".
            if "node_modules/" not in path:
                # The empty key represents the root package; principals get
                # listed there.
                if path == "":
                    continue
                # Workspace member entries (e.g. "api") are skipped — they
                # describe a project, not an installed package.
                continue
            name = path.rsplit("node_modules/", 1)[1]
            for dep in (info.get("dependencies") or {}).keys():
                required_by.setdefault(dep, set()).add(name)
            for dep in (info.get("peerDependencies") or {}).keys():
                required_by.setdefault(dep, set()).add(name)
            for dep in (info.get("optionalDependencies") or {}).keys():
                required_by.setdefault(dep, set()).add(name)
        return required_by

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
        if not actions:
            return []
        log: list[str] = []
        root_pkg_path = self.root / "package.json"
        EcosystemError.require_condition(root_pkg_path.is_file(), f"No package.json at project root: {root_pkg_path}")

        root_pkg = json.loads(root_pkg_path.read_text())
        deps = root_pkg.setdefault("dependencies", {})

        for action in actions:
            deps[action.package] = action.version
            log.append(f"pinned {action.package} -> {action.version}")

        root_pkg_path.write_text(json.dumps(root_pkg, indent=2) + "\n")

        result = subprocess.run(["npm", "install"], cwd=self.root, capture_output=True, text=True)
        if result.returncode != 0:
            raise EcosystemError(f"`npm install` failed after applying fixes: {result.stderr.strip()}")
        log.append("ran: npm install")
        return log

"""
npm ecosystem backend.

Reads installed packages from ``npm list --json`` and from ``package-lock.json``
for transitive resolution. Talks to the npm registry. Applies fixes by editing
``package.json`` (overrides for transitive pins, dependencies for direct pins)
and re-running ``npm install``.
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

    def _read_package_jsons(self) -> tuple[set[str], dict[str, str], set[str]]:
        """
        Read every non-vendored ``package.json`` under the project root.

        Returns:
            A tuple of ``(declared_dep_names, workspace_for, workspace_names)``.
        """
        dep_names: set[str] = set()
        workspace_for: dict[str, str] = {}
        workspace_names: set[str] = set()
        for pkg_json in self.root.glob("**/package.json"):
            if "node_modules" in pkg_json.parts:
                continue
            try:
                doc = json.loads(pkg_json.read_text())
            except json.JSONDecodeError:
                logger.warning(f"Skipping unreadable package.json: {pkg_json}")
                continue
            label = doc.get("name") or str(pkg_json.parent.relative_to(self.root))
            if pkg_json.parent != self.root and doc.get("name"):
                workspace_names.add(doc["name"])
            for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                for name in doc.get(section, {}) or {}:
                    dep_names.add(name)
                    workspace_for.setdefault(name, label)
        return dep_names, workspace_for, workspace_names

    def _load_direct(self) -> list[InstalledPackage]:
        dep_names, workspace_for, workspace_names = self._read_package_jsons()
        data = self._npm_list(depth=1)

        packages: dict[tuple[str, str | None], InstalledPackage] = {}

        def register(name: str, info: dict[str, Any], workspace: str | None) -> None:
            if name not in dep_names or name in workspace_names:
                return
            if str(info.get("resolved", "")).startswith("file:"):
                return
            version = info.get("version")
            if not version:
                return
            ws = workspace or workspace_for.get(name)
            key = (name, ws)
            if key not in packages:
                packages[key] = InstalledPackage(
                    name=name,
                    version=version,
                    ecosystem=self.kind,
                    workspace=ws,
                )

        for name, info in (data.get("dependencies") or {}).items():
            if str(info.get("resolved", "")).startswith("file:"):
                ws_label = info.get("name") or name
                for dep_name, dep_info in (info.get("dependencies") or {}).items():
                    register(dep_name, dep_info, ws_label)
            else:
                register(name, info, None)

        return list(packages.values())

    def _load_all(self) -> list[InstalledPackage]:
        dep_names, _, workspace_names = self._read_package_jsons()
        data = self._npm_list(depth=None)

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

        packages: dict[tuple[str, str | None], InstalledPackage] = {}

        def collect(node: dict[str, Any], workspace: str | None) -> None:
            for name, info in (node.get("dependencies") or {}).items():
                if name in workspace_names:
                    continue
                version = info.get("version")
                if not version:
                    continue
                key = (name, workspace)
                if key not in packages:
                    via_chain = () if name in dep_names else find_via_chain(name)
                    packages[key] = InstalledPackage(
                        name=name,
                        version=version,
                        ecosystem=self.kind,
                        workspace=workspace,
                        via_chain=via_chain,
                    )
                collect(info, workspace)

        for name, info in (data.get("dependencies") or {}).items():
            if str(info.get("resolved", "")).startswith("file:"):
                collect(info, info.get("name") or name)
            else:
                collect({"dependencies": {name: info}}, None)

        return list(packages.values())

    def _build_required_by(self) -> dict[str, set[str]]:
        lock_path = self.root / "package-lock.json"
        if not lock_path.is_file():
            return {}
        try:
            lock = json.loads(lock_path.read_text())
        except json.JSONDecodeError:
            return {}
        required_by: dict[str, set[str]] = {}
        for path, info in (lock.get("packages") or {}).items():
            if not path.startswith("node_modules/"):
                continue
            name = path.removeprefix("node_modules/")
            for dep in (info.get("dependencies") or {}).keys():
                required_by.setdefault(dep, set()).add(name)
            for dep in (info.get("peerDependencies") or {}).keys():
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
        overrides = root_pkg.setdefault("overrides", {})
        installs: list[FixAction] = []

        for action in actions:
            if action.is_override:
                overrides[action.package] = action.version
                log.append(f"override {action.package} -> {action.version}")
            else:
                installs.append(action)
                deps = root_pkg.setdefault("dependencies", {})
                deps[action.package] = action.version
                log.append(f"dependency {action.package} -> {action.version}")

        root_pkg_path.write_text(json.dumps(root_pkg, indent=2) + "\n")

        # A single `npm install` will apply overrides + new deps in one go.
        result = subprocess.run(["npm", "install"], cwd=self.root, capture_output=True, text=True)
        if result.returncode != 0:
            raise EcosystemError(f"`npm install` failed after applying fixes: {result.stderr.strip()}")
        log.append("ran: npm install")
        return log

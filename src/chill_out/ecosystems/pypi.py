"""
PyPI ecosystem backend.

Reads installed packages from ``uv.lock`` (preferred) or from the
``[project.dependencies]`` / ``[dependency-groups.dev]`` tables in
``pyproject.toml``. Talks to the PyPI JSON API for release timestamps. Applies
fixes by editing ``pyproject.toml`` to pin versions and re-running ``uv lock``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pendulum
import tomlkit
from loguru import logger
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from chill_out.constants import EcosystemKind
from chill_out.ecosystems.base import Ecosystem, RegistryClient
from chill_out.exceptions import EcosystemError, RegistryError
from chill_out.models import FixAction, InstalledPackage, PackageInfo, PackageRelease, VersionManifest

PYPI_REGISTRY = "https://pypi.org/pypi"


class PypiRegistryClient(RegistryClient):
    """Async client for the public PyPI JSON API."""

    base_url: str = PYPI_REGISTRY

    async def fetch_package(self, name: str) -> PackageInfo | None:
        """
        Fetch all releases and their upload timestamps for a package.

        The PyPI JSON API returns one entry per uploaded artifact; we take the
        earliest upload time for each version as its publish date.
        """
        url = f"{self.base_url}/{name}/json"
        try:
            res = await self.http.get(url)
        except httpx.TransportError as exc:
            raise RegistryError(f"PyPI transport error for {name}: {exc}") from exc
        if res.status_code == 404:
            return None
        if res.status_code != 200:
            raise RegistryError(f"PyPI returned HTTP {res.status_code} for {name}")
        try:
            data = res.json()
        except json.JSONDecodeError as exc:
            raise RegistryError(f"PyPI returned non-JSON body for {name}: {exc}") from exc

        releases: dict[str, PackageRelease] = {}
        for ver, files in (data.get("releases") or {}).items():
            if not files:
                continue
            stamps: list[pendulum.DateTime] = []
            for entry in files:
                ts = entry.get("upload_time_iso_8601") or entry.get("upload_time")
                if not ts:
                    continue
                try:
                    parsed = pendulum.parse(ts)
                except (ValueError, TypeError):
                    continue
                if isinstance(parsed, pendulum.DateTime):
                    stamps.append(parsed)
            if stamps:
                releases[ver] = PackageRelease(version=ver, published=min(stamps))
        return PackageInfo(name=name, releases=releases)

    async def fetch_version_manifest(self, name: str, version: str) -> VersionManifest | None:
        """
        Fetch the dependency declarations for a single PyPI release.

        Pulls ``info.requires_dist`` from the per-version JSON endpoint. Markers
        that gate a requirement on an ``extra`` are skipped: those represent
        optional installs and don't constrain the base resolution.
        """
        url = f"{self.base_url}/{name}/{version}/json"
        try:
            res = await self.http.get(url)
        except httpx.TransportError as exc:
            raise RegistryError(f"PyPI transport error for {name}=={version}: {exc}") from exc
        if res.status_code == 404:
            return None
        if res.status_code != 200:
            raise RegistryError(f"PyPI returned HTTP {res.status_code} for {name}=={version}")
        try:
            data = res.json()
        except json.JSONDecodeError as exc:
            raise RegistryError(f"PyPI returned non-JSON body for {name}=={version}: {exc}") from exc

        deps: dict[str, str] = {}
        for raw in (data.get("info", {}) or {}).get("requires_dist") or []:
            try:
                req = Requirement(raw)
            except InvalidRequirement:
                logger.warning(f"Skipping unparsable requires_dist entry for {name}=={version}: {raw!r}")
                continue
            # Skip optional-extra requirements; they only apply when the parent
            # is installed with that extra, which we don't track.
            if req.marker is not None and "extra" in str(req.marker):
                continue
            deps[req.name] = str(req.specifier) if req.specifier else ""
        return VersionManifest(name=name, version=version, deps=deps)


class PypiEcosystem(Ecosystem):
    """Ecosystem backend for Python projects using uv + pyproject.toml."""

    kind: EcosystemKind = EcosystemKind.PYPI

    @classmethod
    def detect(cls, root: Path) -> bool:
        return (root / "pyproject.toml").is_file()

    def make_client(self, http: httpx.AsyncClient) -> RegistryClient:
        return PypiRegistryClient(http)

    # ------------------------------------------------------------------
    # Installed package enumeration
    # ------------------------------------------------------------------

    def load_installed(self, *, deep: bool = False) -> list[InstalledPackage]:
        # Direct deps live in pyproject.toml (project.dependencies + dependency-groups).
        direct_specs = self._read_direct_specs()
        if deep:
            return self._load_from_lock(direct_specs)
        return self._resolve_direct(direct_specs)

    def _read_direct_specs(self) -> dict[str, str]:
        """
        Return a map of ``normalized_name -> raw_spec_string`` for every direct
        dependency declared in pyproject.toml.
        """
        path = self.root / "pyproject.toml"
        EcosystemError.require_condition(path.is_file(), f"No pyproject.toml at {path}")
        doc = tomlkit.parse(path.read_text())

        specs: dict[str, str] = {}

        def absorb(items: Any) -> None:
            if not items:
                return
            for raw in items:
                try:
                    req = Requirement(str(raw))
                except InvalidRequirement:
                    logger.warning(f"Skipping unparsable requirement: {raw!r}")
                    continue
                specs[_normalize(req.name)] = str(raw)

        project = doc.get("project", {})
        if isinstance(project, dict):
            absorb(project.get("dependencies"))
            for extras in (project.get("optional-dependencies") or {}).values():
                absorb(extras)

        groups = doc.get("dependency-groups", {})
        if isinstance(groups, dict):
            for items in groups.values():
                absorb(items)

        return specs

    def _resolve_direct(self, direct_specs: dict[str, str]) -> list[InstalledPackage]:
        """
        Pair each direct dep with the version that uv resolved for it in the lockfile.

        If there is no uv.lock, fall back to a heuristic: extract a pinned
        version from the spec if one is given (e.g. ``foo==1.2.3``).
        """
        lock_versions = self._read_lock_versions()
        out: list[InstalledPackage] = []
        for normalized, raw in direct_specs.items():
            version = lock_versions.get(normalized)
            if version is None:
                version = _extract_pinned_version(raw)
            if version is None:
                logger.warning(f"No resolved version for {raw!r}; skipping")
                continue
            out.append(
                InstalledPackage(
                    name=normalized,
                    version=version,
                    ecosystem=self.kind,
                )
            )
        return out

    def _load_from_lock(self, direct_specs: dict[str, str]) -> list[InstalledPackage]:
        """
        Enumerate every package in uv.lock, attributing each transitive dep to a
        principal direct dependency via reverse-graph BFS.
        """
        lock_path = self.root / "uv.lock"
        EcosystemError.require_condition(
            lock_path.is_file(),
            "Cannot enumerate transitive deps without uv.lock; run `uv lock` first.",
        )
        doc = tomlkit.parse(lock_path.read_text())
        packages = doc.get("package") or []
        version_by_name: dict[str, str] = {}
        deps_by_name: dict[str, set[str]] = {}
        for pkg in packages:
            name = _normalize(pkg.get("name", ""))
            if not name:
                continue
            version_by_name[name] = pkg.get("version", "")
            deps = set()
            for dep in pkg.get("dependencies") or []:
                dep_name = _normalize(dep.get("name", ""))
                if dep_name:
                    deps.add(dep_name)
            deps_by_name[name] = deps

        # Build reverse-graph for via-chain attribution.
        required_by: dict[str, set[str]] = {}
        for parent, deps in deps_by_name.items():
            for dep in deps:
                required_by.setdefault(dep, set()).add(parent)

        principals = set(direct_specs.keys())

        def find_via_chain(name: str) -> tuple[str, ...]:
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

        out: list[InstalledPackage] = []
        for name, version in version_by_name.items():
            if not version:
                continue
            via_chain = () if name in principals else find_via_chain(name)
            out.append(
                InstalledPackage(
                    name=name,
                    version=version,
                    ecosystem=self.kind,
                    via_chain=via_chain,
                )
            )
        return out

    def _read_lock_versions(self) -> dict[str, str]:
        lock_path = self.root / "uv.lock"
        if not lock_path.is_file():
            return {}
        try:
            doc = tomlkit.parse(lock_path.read_text())
        except Exception:  # noqa: BLE001
            return {}
        out: dict[str, str] = {}
        for pkg in doc.get("package") or []:
            name = _normalize(pkg.get("name", ""))
            if name:
                out[name] = pkg.get("version", "")
        return out

    # ------------------------------------------------------------------
    # Fix application
    # ------------------------------------------------------------------

    def range_satisfies(self, version: str, range_spec: str) -> bool:
        """
        Return True if ``version`` satisfies a PEP 440 ``range_spec``.

        An empty or whitespace-only range matches any version (matches
        ``packaging``'s ``SpecifierSet("")`` semantics). Unparsable inputs are
        treated permissively to match the original script's "assume compatible"
        behavior for transitive deps with no discoverable range.
        """
        if not range_spec or not range_spec.strip():
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

    def apply_fixes(self, actions: list[FixAction]) -> list[str]:
        if not actions:
            return []
        path = self.root / "pyproject.toml"
        EcosystemError.require_condition(path.is_file(), f"No pyproject.toml at {path}")
        doc = tomlkit.parse(path.read_text())
        log: list[str] = []

        for action in actions:
            replaced = _pin_dependency(doc, action.package, action.version)
            if replaced:
                log.append(f"pinned {action.package} -> {action.version}")
            else:
                # Add to project.dependencies if it isn't declared anywhere.
                project = doc.setdefault("project", tomlkit.table())
                deps = project.setdefault("dependencies", tomlkit.array())
                deps.append(f"{action.package}=={action.version}")
                log.append(f"added {action.package}=={action.version} to project.dependencies")

        path.write_text(tomlkit.dumps(doc))

        result = subprocess.run(["uv", "lock"], cwd=self.root, capture_output=True, text=True)
        if result.returncode != 0:
            raise EcosystemError(f"`uv lock` failed after applying fixes: {result.stderr.strip()}")
        log.append("ran: uv lock")
        return log


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize(name: str) -> str:
    """PEP 503 name normalization."""
    import re

    return re.sub(r"[-_.]+", "-", name).lower()


def _extract_pinned_version(spec: str) -> str | None:
    """Best-effort extraction of a pinned version from a requirement spec."""
    try:
        req = Requirement(spec)
    except InvalidRequirement:
        return None
    for sp in req.specifier:
        if sp.operator == "==":
            return sp.version
    return None


def _pin_dependency(doc: Any, package: str, version: str) -> bool:
    """
    Replace any existing requirement string for ``package`` with ``package==version``
    in either ``project.dependencies``, ``project.optional-dependencies``, or
    ``dependency-groups``. Returns True if anything was replaced.
    """
    target = _normalize(package)
    new_spec = f"{package}=={version}"
    replaced = False

    def rewrite(items: Any) -> None:
        nonlocal replaced
        if items is None:
            return
        for idx in range(len(items)):
            raw = str(items[idx])
            try:
                req = Requirement(raw)
            except InvalidRequirement:
                continue
            if _normalize(req.name) == target:
                items[idx] = new_spec
                replaced = True

    project = doc.get("project")
    if isinstance(project, dict):
        rewrite(project.get("dependencies"))
        for extras in (project.get("optional-dependencies") or {}).values():
            rewrite(extras)

    groups = doc.get("dependency-groups")
    if isinstance(groups, dict):
        for items in groups.values():
            rewrite(items)

    return replaced

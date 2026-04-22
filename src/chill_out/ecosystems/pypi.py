"""
PyPI ecosystem backend.

Reads installed packages from ``uv.lock`` (preferred) or from the
``[project.dependencies]`` / ``[dependency-groups.dev]`` tables in
``pyproject.toml``. Talks to the PyPI JSON API for release timestamps. Applies
fixes by editing ``pyproject.toml`` to pin versions and re-running ``uv lock``.
"""

from __future__ import annotations

import glob
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

from chill_out.constants import DependencyGroup, EcosystemKind
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

    def _read_direct_specs(self) -> dict[str, tuple[str, set[DependencyGroup]]]:
        """
        Return a map of ``normalized_name -> (raw_spec_string, groups)`` for
        every direct dependency declared in pyproject.toml.

        Group attribution mirrors the conventional pypi layout:

        * ``[project.dependencies]`` -> :attr:`DependencyGroup.MAIN`
        * ``[dependency-groups.dev]`` and
          ``[project.optional-dependencies.dev]`` -> :attr:`DependencyGroup.DEV`
        * Every other ``[project.optional-dependencies.*]`` extra and every
          other ``[dependency-groups.*]`` group -> :attr:`DependencyGroup.OPTIONAL`

        A package declared in more than one section accumulates every
        matching group.
        """
        path = self.root / "pyproject.toml"
        EcosystemError.require_condition(path.is_file(), f"No pyproject.toml at {path}")
        doc = tomlkit.parse(path.read_text())

        specs: dict[str, tuple[str, set[DependencyGroup]]] = {}

        def absorb(items: Any, group: DependencyGroup) -> None:
            if not items:
                return
            for raw in items:
                try:
                    req = Requirement(str(raw))
                except InvalidRequirement:
                    logger.warning(f"Skipping unparsable requirement: {raw!r}")
                    continue
                normalized = _normalize(req.name)
                existing = specs.get(normalized)
                if existing is None:
                    specs[normalized] = (str(raw), {group})
                else:
                    existing[1].add(group)

        project = doc.get("project", {})
        if isinstance(project, dict):
            absorb(project.get("dependencies"), DependencyGroup.MAIN)
            for extra_name, extras in (project.get("optional-dependencies") or {}).items():
                group = DependencyGroup.DEV if str(extra_name) == "dev" else DependencyGroup.OPTIONAL
                absorb(extras, group)

        groups = doc.get("dependency-groups", {})
        if isinstance(groups, dict):
            for group_name, items in groups.items():
                semantic = DependencyGroup.DEV if str(group_name) == "dev" else DependencyGroup.OPTIONAL
                absorb(items, semantic)

        return specs

    def _resolve_direct(
        self, direct_specs: dict[str, tuple[str, set[DependencyGroup]]]
    ) -> list[InstalledPackage]:
        """
        Pair each direct dep with the version that uv resolved for it in the lockfile.

        If there is no uv.lock, fall back to a heuristic: extract a pinned
        version from the spec if one is given (e.g. ``foo==1.2.3``).
        """
        lock_versions = self._read_lock_versions()
        out: list[InstalledPackage] = []
        for normalized, (raw, dep_groups) in direct_specs.items():
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
                    groups=tuple(sorted(dep_groups, key=lambda g: g.value)),
                )
            )
        return out

    def _load_from_lock(
        self, direct_specs: dict[str, tuple[str, set[DependencyGroup]]]
    ) -> list[InstalledPackage]:
        """
        Enumerate every package in uv.lock, attributing each transitive dep to
        a principal direct dependency via reverse-graph BFS.

        Group attribution for transitives follows the same union semantic as
        npm's deep mode: forward-walk the dependency graph from each
        principal and tag every reachable package with that principal's
        groups. Transitives reached through multiple principals accumulate
        the union, matching the runner's "included if reachable through any
        included group" rule.
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

        # Forward-walk from each principal to attribute groups to every
        # reachable transitive. Done as a per-principal BFS so each starting
        # group label propagates independently and the union accumulates
        # naturally.
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

        out: list[InstalledPackage] = []
        for name, version in version_by_name.items():
            if not version:
                continue
            via_chain = () if name in principals else find_via_chain(name)
            pkg_groups = tuple(
                sorted(groups_by_pkg.get(name, set()), key=lambda g: g.value)
            )
            out.append(
                InstalledPackage(
                    name=name,
                    version=version,
                    ecosystem=self.kind,
                    via_chain=via_chain,
                    groups=pkg_groups,
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

    def workspace_topology(self) -> WorkspaceTopology | None:
        """
        Detect a uv workspace by walking up to find a ``pyproject.toml`` with ``[tool.uv.workspace]``.

        Starts at ``self.root`` and walks toward the filesystem root until
        it finds a ``pyproject.toml`` declaring ``[tool.uv.workspace]``.
        Reads ``members`` (glob patterns) and ``exclude``, expands the
        globs, and returns a :class:`WorkspaceTopology` keyed by each
        member's ``project.name``.

        Returns ``None`` when no workspace declaration is reachable from
        ``self.root``.
        """
        cur = self.root.resolve()
        ws_doc: dict[str, Any] | None = None
        ws_root: Path | None = None
        while True:
            candidate = cur / "pyproject.toml"
            if candidate.is_file():
                try:
                    doc = tomlkit.parse(candidate.read_text())
                except Exception:
                    doc = None
                if doc is not None:
                    ws = (
                        doc.get("tool", {}) if isinstance(doc.get("tool"), dict) else {}
                    ).get("uv", {})
                    if isinstance(ws, dict) and isinstance(ws.get("workspace"), dict):
                        ws_doc = ws["workspace"]
                        ws_root = cur
                        break
            if cur == cur.parent:
                break
            cur = cur.parent

        if ws_doc is None or ws_root is None:
            return None

        members_field = ws_doc.get("members") or []
        exclude_field = ws_doc.get("exclude") or []
        excluded: set[Path] = set()
        for pattern in exclude_field:
            for match in glob.glob(str(ws_root / pattern)):
                excluded.add(Path(match).resolve())

        members: dict[str, Path] = {}
        for pattern in members_field:
            for match in glob.glob(str(ws_root / pattern)):
                member_dir = Path(match)
                if not member_dir.is_dir():
                    continue
                if member_dir.resolve() in excluded:
                    continue
                member_pyproject = member_dir / "pyproject.toml"
                if not member_pyproject.is_file():
                    continue
                try:
                    member_doc = tomlkit.parse(member_pyproject.read_text())
                except Exception:
                    continue
                project = member_doc.get("project") if isinstance(member_doc.get("project"), dict) else None
                if not project:
                    continue
                name = project.get("name")
                if not name:
                    continue
                members[_normalize(str(name))] = member_dir
        if not members:
            return None
        return WorkspaceTopology(root=ws_root, members=members)

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
        """Apply pins. Routes ``via_overrides`` actions through ``apply_override_fixes``.

        Direct pins are written into ``self.root``'s ``pyproject.toml`` and
        validated with ``uv lock``. Override pins go through the workspace
        root's ``[tool.uv].override-dependencies`` field and trigger a
        workspace-wide ``uv lock`` to recompute the resolution.
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
                logger.warning("override path unavailable; falling back to direct pins for shared actions")
                log.extend(self._apply_direct_fixes(override_actions))
            else:
                log.extend(override_log)
        return log

    def _apply_direct_fixes(self, actions: list[FixAction]) -> list[str]:
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

    def supports_overrides(self) -> bool:
        return True

    def apply_override_fixes(self, actions: list[FixAction]) -> list[str] | None:
        """
        Force transitive versions via uv's ``override-dependencies`` mechanism.

        Writes one entry per action to ``[tool.uv].override-dependencies``
        in the workspace root's ``pyproject.toml`` (or ``self.root`` when
        there's no workspace), then runs ``uv lock`` from that directory
        to recompute the workspace-wide resolution. Returns the log on
        success, or ``None`` when no usable workspace root could be
        located.
        """
        if not actions:
            return []
        topology = self.workspace_topology()
        write_root = topology.root if topology is not None else self.root
        path = write_root / "pyproject.toml"
        if not path.is_file():
            return None

        doc = tomlkit.parse(path.read_text())
        tool = doc.setdefault("tool", tomlkit.table())
        uv_section = tool.setdefault("uv", tomlkit.table())
        overrides = uv_section.get("override-dependencies")
        if overrides is None:
            overrides = tomlkit.array()
            uv_section["override-dependencies"] = overrides

        log: list[str] = []
        # Drop any existing entries we're replacing so we don't accumulate
        # duplicate pins on re-runs.
        existing_specs = list(overrides)
        kept: list[str] = []
        for raw in existing_specs:
            try:
                req = Requirement(str(raw))
                replaced = any(_normalize(req.name) == _normalize(a.package) for a in actions)
            except InvalidRequirement:
                replaced = False
            if not replaced:
                kept.append(str(raw))
        # Rebuild the array in place.
        while len(overrides) > 0:
            overrides.pop()
        for raw in kept:
            overrides.append(raw)
        for action in actions:
            spec = f"{action.package}=={action.version}"
            overrides.append(spec)
            log.append(f"overrode {spec} (workspace root)")

        path.write_text(tomlkit.dumps(doc))

        result = subprocess.run(["uv", "lock"], cwd=write_root, capture_output=True, text=True)
        if result.returncode != 0:
            raise EcosystemError(f"`uv lock` failed after applying overrides: {result.stderr.strip()}")
        log.append(f"ran: uv lock (in {write_root})")
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

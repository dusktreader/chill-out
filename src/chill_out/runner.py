"""
Top-level orchestration for the chill-out check workflow.

Combines an :class:`Ecosystem` backend with the cooldown logic to produce a
:class:`CheckReport` and, optionally, a list of :class:`FixAction`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path

import httpx
import pendulum
from loguru import logger

from chill_out.cache import CachingRegistryClient
from chill_out.config import CooldownConfig, load_config
from chill_out.constants import DEFAULT_CONCURRENCY, DEFAULT_TIMEOUT, EcosystemKind
from chill_out.cooldown import find_safe_principal_version, find_safe_version, is_within_cooldown, release_type
from chill_out.ecosystems import detect_ecosystem, get_ecosystem
from chill_out.ecosystems.base import Ecosystem, RegistryClient
from chill_out.exceptions import RegistryError
from chill_out.models import CheckReport, FixAction, InstalledPackage, PackageInfo, Violation


async def _check_one(
    pkg: InstalledPackage,
    client: RegistryClient,
    config: CooldownConfig,
    semaphore: asyncio.Semaphore,
    *,
    fast: bool,
    now: pendulum.DateTime,
) -> tuple[InstalledPackage, Violation | str | None]:
    """
    Fetch and evaluate a single package.

    Returns:
        ``(pkg, Violation)`` if the package is in cooldown,
        ``(pkg, "skip reason")`` if it could not be evaluated,
        ``(pkg, None)`` if it has cleared cooldown.
    """
    async with semaphore:
        try:
            info = await client.fetch_package(pkg.name)
        except RegistryError as exc:
            logger.warning(f"Skipping {pkg.name}: {exc}")
            return pkg, str(exc)
        if info is None:
            return pkg, "not found in registry"
        published = info.published_at(pkg.version)
        if published is None:
            return pkg, f"no publish date for {pkg.version}"
        bump = release_type(pkg.version)
        violating, age_days, limit_days = is_within_cooldown(published, bump, config, now=now)
        if not violating:
            return pkg, None
        safe = None if fast else find_safe_version(pkg.version, info, config, now=now)
        return pkg, Violation(
            package=pkg,
            bump=bump,
            age_days=age_days,
            limit_days=limit_days,
            published=published,
            safe_version=safe,
        )


async def check_async(
    ecosystem: Ecosystem,
    *,
    config: CooldownConfig | None = None,
    deep: bool = False,
    fast: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
    http: httpx.AsyncClient | None = None,
    now: pendulum.DateTime | None = None,
) -> CheckReport:
    """
    Run the full cooldown check for the given ecosystem.

    Args:
        ecosystem: The detected or selected ecosystem backend.
        config: Cooldown configuration. If omitted, it is loaded from the
            ecosystem's project root.
        deep: If True, include transitive dependencies in the check.
        fast: If True, skip the safe-version lookup for faster runs.
        concurrency: Maximum simultaneous registry requests.
        http: Optional pre-configured HTTP client (mostly useful for testing).
        now: Override the "now" timestamp used when comparing ages (testing).
    """
    config = config or load_config(ecosystem.root, ecosystem.kind)
    now = now or pendulum.now("UTC")
    packages = ecosystem.load_installed(deep=deep)
    semaphore = asyncio.Semaphore(concurrency)

    own_http = http is None
    if own_http:
        http = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
    assert http is not None
    try:
        client = CachingRegistryClient(ecosystem.make_client(http))
        results = await asyncio.gather(
            *(_check_one(pkg, client, config, semaphore, fast=fast, now=now) for pkg in packages)
        )
    finally:
        if own_http:
            await http.aclose()

    report = CheckReport(ecosystem=ecosystem.kind, checked=list(packages))
    for pkg, outcome in results:
        if isinstance(outcome, Violation):
            report.violations.append(outcome)
        elif isinstance(outcome, str):
            report.skipped.append((pkg, outcome))
    return report


def check(
    root: Path,
    *,
    ecosystem_kind: EcosystemKind | None = None,
    config: CooldownConfig | None = None,
    deep: bool = False,
    fast: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> CheckReport:
    """
    Synchronous convenience wrapper around :func:`check_async`.

    Auto-detects the ecosystem from ``root`` unless ``ecosystem_kind`` is given.
    """
    ecosystem = get_ecosystem(ecosystem_kind, root) if ecosystem_kind else detect_ecosystem(root)
    return asyncio.run(
        check_async(
            ecosystem,
            config=config,
            deep=deep,
            fast=fast,
            concurrency=concurrency,
        )
    )


def plan_fixes(report: CheckReport) -> list[FixAction]:
    """
    Build a basic fix plan from a report, without principal rollback.

    Each violation with a known safe version becomes one :class:`FixAction`.
    Transitive violations are emitted as overrides; principal violations as
    direct dependency pins. For the smarter version that may also roll back a
    principal whose declared range is incompatible with the safe transitive,
    use :func:`plan_fixes_async`.
    """
    actions: list[FixAction] = []
    for v in report.violations:
        if v.safe_version is None:
            continue
        actions.append(
            FixAction(
                package=v.name,
                version=v.safe_version.version,
                workspace=v.workspace,
                is_override=bool(v.via),
            )
        )
    return _dedupe_actions(actions)


async def plan_fixes_async(
    report: CheckReport,
    ecosystem: Ecosystem,
    *,
    config: CooldownConfig | None = None,
    http: httpx.AsyncClient | None = None,
    now: pendulum.DateTime | None = None,
) -> list[FixAction]:
    """
    Build a fix plan, rolling back principal versions when their declared range
    can't admit the safe transitive version.

    For each transitive violation the runner:

    1. Looks up the principal's installed version in ``report.checked``.
    2. Fetches the principal's manifest at that installed version. If the
       declared range for the transitive already accepts the safe version,
       emits just the override (same as :func:`plan_fixes`).
    3. Otherwise, searches for an older principal version that has cleared
       its own cooldown and whose declared range *does* admit the safe
       transitive. Emits both an install action for the principal rollback
       and an override for the transitive.

    If no compatible principal can be found, the violation is skipped (the
    user can still fix it manually). Principal violations are emitted as plain
    install actions, identical to :func:`plan_fixes`.
    """
    config = config or load_config(ecosystem.root, ecosystem.kind)
    now = now or pendulum.now("UTC")

    own_http = http is None
    if own_http:
        http = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
    assert http is not None
    client = CachingRegistryClient(ecosystem.make_client(http))

    installed_by_key: dict[tuple[str, str | None], InstalledPackage] = {
        (p.name, p.workspace): p for p in report.checked
    }

    actions: list[FixAction] = []
    try:
        for v in report.violations:
            if v.safe_version is None:
                continue
            if not v.via:
                actions.append(
                    FixAction(
                        package=v.name,
                        version=v.safe_version.version,
                        workspace=v.workspace,
                    )
                )
                continue
            principal_pkg = installed_by_key.get((v.via, v.workspace))
            if principal_pkg is None:
                actions.append(
                    FixAction(
                        package=v.name,
                        version=v.safe_version.version,
                        workspace=v.workspace,
                        is_override=True,
                    )
                )
                continue

            installed_manifest = await client.fetch_version_manifest(v.via, principal_pkg.version)
            installed_range = installed_manifest.deps.get(v.name) if installed_manifest else None
            if installed_range is None or ecosystem.range_satisfies(v.safe_version.version, installed_range):
                actions.append(
                    FixAction(
                        package=v.name,
                        version=v.safe_version.version,
                        workspace=v.workspace,
                        is_override=True,
                    )
                )
                continue

            principal_info = await client.fetch_package(v.via)
            if principal_info is None:
                logger.warning(f"Cannot roll back principal {v.via}: registry lookup failed")
                continue
            candidate_versions = _candidate_principal_versions(principal_info, principal_pkg.version, config, now)
            fetched = await asyncio.gather(*(client.fetch_version_manifest(v.via, ver) for ver in candidate_versions))
            manifests = {ver: m for ver, m in zip(candidate_versions, fetched, strict=True) if m is not None}
            principal_safe = find_safe_principal_version(
                principal_pkg.version,
                principal_info,
                manifests,
                v.name,
                v.safe_version,
                ecosystem.range_satisfies,
                config,
                now=now,
            )
            if principal_safe is None:
                logger.warning(
                    f"Cannot roll back principal {v.via} to admit {v.name}@{v.safe_version.version}; skipping"
                )
                continue
            actions.append(
                FixAction(
                    package=v.via,
                    version=principal_safe.version,
                    workspace=v.workspace,
                )
            )
            actions.append(
                FixAction(
                    package=v.name,
                    version=v.safe_version.version,
                    workspace=v.workspace,
                    is_override=True,
                )
            )
    finally:
        if own_http:
            await http.aclose()

    return _dedupe_actions(actions)


def _candidate_principal_versions(
    info: PackageInfo,
    installed_version: str,
    config: CooldownConfig,
    now: pendulum.DateTime,
) -> list[str]:
    """
    Pick the set of principal versions worth fetching manifests for.

    Strict subset of ``find_safe_principal_version``'s candidate filter that
    avoids the manifest fetch (which is only needed for the ones that survive
    the cooldown filter).
    """
    from chill_out.cooldown import parse_version

    current_v = parse_version(installed_version)
    if current_v is None:
        return []
    out: list[str] = []
    for ver_str, release in info.releases.items():
        v = parse_version(ver_str)
        if v is None or v >= current_v or v.prerelease:
            continue
        bump = release_type(ver_str)
        violating, _, _ = is_within_cooldown(release.published, bump, config, now=now)
        if violating:
            continue
        out.append(ver_str)
    return out


def _dedupe_actions(actions: Iterable[FixAction]) -> list[FixAction]:
    """Deduplicate by (package, workspace), keeping the smallest version."""
    from packaging.version import InvalidVersion, Version

    out: dict[tuple[str, str | None], FixAction] = {}
    for a in actions:
        key = (a.package, a.workspace)
        existing = out.get(key)
        if existing is None:
            out[key] = a
            continue
        try:
            if Version(a.version) < Version(existing.version):
                out[key] = a
        except InvalidVersion:
            # Fall back to lexical comparison if the versions aren't PEP 440.
            if a.version < existing.version:
                out[key] = a
    return list(out.values())

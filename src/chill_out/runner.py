"""
Top-level orchestration for the chill-out check workflow.

Combines an :class:`Ecosystem` backend with the cooldown logic to produce a
:class:`CheckReport` and, optionally, a list of :class:`FixAction`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
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
from chill_out.models import (
    CheckReport,
    FixAction,
    FixPlan,
    InstalledPackage,
    PackageInfo,
    UnfixableViolation,
    Violation,
)


async def _check_one(
    pkg: InstalledPackage,
    client: RegistryClient,
    config: CooldownConfig,
    semaphore: asyncio.Semaphore,
    *,
    fast: bool,
    now: pendulum.DateTime,
    on_complete: Callable[[InstalledPackage], None] | None = None,
) -> tuple[InstalledPackage, Violation | str | None]:
    """
    Fetch and evaluate a single package.

    Returns:
        ``(pkg, Violation)`` if the package is in cooldown,
        ``(pkg, "skip reason")`` if it could not be evaluated,
        ``(pkg, None)`` if it has cleared cooldown.

    The ``on_complete`` callback fires once the package has been evaluated,
    regardless of outcome. Useful for wiring up progress reporting without
    coupling the runner to a particular UI library.
    """
    async with semaphore:
        try:
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
            rel_type = release_type(pkg.version)
            violating, age_days, limit_days = is_within_cooldown(published, rel_type, config, now=now)
            if not violating:
                return pkg, None
            safe = None if fast else find_safe_version(pkg.version, info, config, now=now)
            return pkg, Violation(
                package=pkg,
                release_type=rel_type,
                age_days=age_days,
                limit_days=limit_days,
                published=published,
                safe_version=safe,
            )
        finally:
            if on_complete is not None:
                on_complete(pkg)


async def check_async(
    ecosystem: Ecosystem,
    *,
    config: CooldownConfig | None = None,
    deep: bool = False,
    fast: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
    http: httpx.AsyncClient | None = None,
    now: pendulum.DateTime | None = None,
    on_start: Callable[[list[InstalledPackage]], None] | None = None,
    on_progress: Callable[[InstalledPackage], None] | None = None,
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
        on_start: Optional callback fired once with the full list of packages
            about to be checked. Use it to size a progress bar.
        on_progress: Optional callback fired once per package after it has
            been evaluated. Use it to advance a progress bar.
    """
    config = config or load_config(ecosystem.root, ecosystem.kind)
    now = now or pendulum.now("UTC")
    packages = ecosystem.load_installed(deep=deep)
    if on_start is not None:
        on_start(list(packages))
    semaphore = asyncio.Semaphore(concurrency)

    own_http = http is None
    if own_http:
        http = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
    assert http is not None
    try:
        client = CachingRegistryClient(ecosystem.make_client(http))
        results = await asyncio.gather(
            *(
                _check_one(pkg, client, config, semaphore, fast=fast, now=now, on_complete=on_progress)
                for pkg in packages
            )
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


def plan_fixes(report: CheckReport) -> FixPlan:
    """
    Build a basic fix plan from a report, without principal range checking.

    Each violation with a known safe version becomes a single :class:`FixAction`
    that pins the package directly (in ``project.dependencies`` for pypi, in
    ``dependencies`` for npm). Transitive violations get pinned as direct deps
    too, so the resolver hoists them and they win over the principal's
    declared range. Violations with no known safe version land in
    :attr:`FixPlan.unfixable` so the caller can report them.

    For the smarter version that range-checks transitive pins against the
    installed principal and rolls the principal back when the declared range
    can't admit the safe transitive, use :func:`plan_fixes_async`.
    """
    plan = FixPlan()
    for v in report.violations:
        if v.safe_version is None:
            plan.unfixable.append(UnfixableViolation(v, "no safe version found within the cooldown window"))
            continue
        plan.actions.append(FixAction(package=v.name, version=v.safe_version.version))
    plan.actions = _dedupe_actions(plan.actions)
    return plan


async def plan_fixes_async(
    report: CheckReport,
    ecosystem: Ecosystem,
    *,
    config: CooldownConfig | None = None,
    http: httpx.AsyncClient | None = None,
    now: pendulum.DateTime | None = None,
) -> FixPlan:
    """
    Build a fix plan with conflict-aware principal rollback.

    Every violation gets pinned as a direct dependency in the project's
    primary manifest. For transitive violations the runner walks every
    ancestor in the chain (immediate parent up through the principal) and
    checks whether any of them declares a range for the violating package
    that excludes the safe version. The flow is:

    1. **Direct violation:** pin the safe version. Done.
    2. **Transitive violation, no ancestor range conflicts:** pin the safe
       version directly. The resolver hoists the direct pin and every
       ancestor stays where it is.
    3. **Transitive violation, an ancestor range conflicts:** search for an
       older principal version (out of cooldown, non-prerelease) whose
       declared range *does* admit the safe transitive. If found, emit pins
       for both the principal rollback and the transitive. If no compatible
       older principal exists, record the violation in ``unfixable`` with a
       structured reason so the caller can show the user their options
       (downgrade the principal manually, raise the safe target, or wait
       out the cooldown).

    The principal is the only level that always gets rolled back because
    it's the only ancestor declared in the project's own manifest. Rolling
    it back changes which intermediate versions the resolver picks, which
    can clear conflicts deeper in the chain.
    """
    config = config or load_config(ecosystem.root, ecosystem.kind)
    now = now or pendulum.now("UTC")

    own_http = http is None
    if own_http:
        http = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
    assert http is not None
    client = CachingRegistryClient(ecosystem.make_client(http))

    installed_by_name: dict[str, InstalledPackage] = {p.name: p for p in report.checked}

    def pin(v: Violation, version: str) -> FixAction:
        """Build the right kind of pin for this violation.

        Shared transitive violations (multiple workspace members pull the
        same install in) need an override-style pin because a member-level
        ``dependencies`` entry can't dislodge a sibling-shared copy. Direct
        violations on the current project's own manifest stay as plain pins
        even when the package happens to be shared, since the user
        explicitly declared it here.
        """
        use_overrides = v.is_shared and bool(v.via)
        return FixAction(package=v.name, version=version, via_overrides=use_overrides)

    plan = FixPlan()
    try:
        for v in report.violations:
            if v.safe_version is None:
                plan.unfixable.append(
                    UnfixableViolation(v, "no safe version found within the cooldown window")
                )
                continue
            if not v.via:
                plan.actions.append(pin(v, v.safe_version.version))
                continue

            principal_pkg = installed_by_name.get(v.via)
            if principal_pkg is None:
                # Principal not in the installed set (rare, but possible if the
                # via attribution races against a manifest edit). Pin the
                # transitive directly; the resolver will figure it out.
                plan.actions.append(pin(v, v.safe_version.version))
                continue

            # Walk every ancestor in the chain (immediate parent first,
            # principal last) and check whether any of them declares a range
            # for this package that excludes the safe version. The earlier
            # version of this code only checked the principal, which misses
            # the common case where a deeply nested intermediate is the one
            # constraining the resolution.
            conflicting_range: str | None = None
            for ancestor_name in v.package.via_chain:
                ancestor_pkg = installed_by_name.get(ancestor_name)
                if ancestor_pkg is None:
                    continue
                ancestor_manifest = await client.fetch_version_manifest(ancestor_name, ancestor_pkg.version)
                ancestor_range = ancestor_manifest.deps.get(v.name) if ancestor_manifest else None
                if ancestor_range is None:
                    continue
                if not ecosystem.range_satisfies(v.safe_version.version, ancestor_range):
                    conflicting_range = ancestor_range
                    break

            if conflicting_range is None:
                # No ancestor's range excludes the safe version. A direct pin
                # will hoist over whatever the resolver picks for the chain.
                plan.actions.append(pin(v, v.safe_version.version))
                continue

            installed_range = conflicting_range

            # Conflict: try to roll the principal back to a version whose
            # declared range admits the safe transitive.
            principal_info = await client.fetch_package(v.via)
            if principal_info is None:
                plan.unfixable.append(
                    UnfixableViolation(
                        v,
                        f"safe version {v.safe_version.version} conflicts with {v.via}@{principal_pkg.version} "
                        f"(declares {v.name}{installed_range}), and the principal's release index could not be "
                        "fetched to look for a rollback target.",
                    )
                )
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
                plan.unfixable.append(
                    UnfixableViolation(
                        v,
                        f"safe version {v.safe_version.version} conflicts with {v.via}@{principal_pkg.version} "
                        f"(declares {v.name}{installed_range}), and no older {v.via} release that has cleared "
                        f"its own cooldown declares a range that admits {v.name}=={v.safe_version.version}. "
                        f"Options: downgrade {v.via} manually, raise the safe target for {v.name}, "
                        "or wait out the cooldown.",
                    )
                )
                continue
            plan.actions.append(FixAction(package=v.via, version=principal_safe.version))
            plan.actions.append(FixAction(package=v.name, version=v.safe_version.version))
    finally:
        if own_http:
            await http.aclose()

    plan.actions = _dedupe_actions(plan.actions)
    return plan


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
        rel_type = release_type(ver_str)
        violating, _, _ = is_within_cooldown(release.published, rel_type, config, now=now)
        if violating:
            continue
        out.append(ver_str)
    return out


def _dedupe_actions(actions: Iterable[FixAction]) -> list[FixAction]:
    """Deduplicate by package name, keeping the smallest version."""
    from packaging.version import InvalidVersion, Version

    out: dict[str, FixAction] = {}
    for a in actions:
        key = a.package
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

"""
Top-level orchestration for the chill-out check workflow.

Combines an `Ecosystem` backend with the cooldown logic to produce a
`CheckReport` and, optionally, a list of `FixAction`.
"""

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pendulum
from loguru import logger

from chill_out.config import ChillOutConfig, load_config
from chill_out.constants import DEFAULT_CONCURRENCY, DEFAULT_TIMEOUT, AuditStatus, EcosystemKind, FixStyle
from chill_out.cooldown import find_safe_principal_version, find_safe_version, is_within_cooldown, release_type
from chill_out.ecosystems import detect_ecosystem, get_ecosystem
from chill_out.ecosystems.backend import Ecosystem
from chill_out.ecosystems.version_parsing import VersionParser
from chill_out.exceptions import ChillOutError, RegistryError
from chill_out.models import (
    AppliedFixes,
    AuditedPin,
    AuditReport,
    CheckReport,
    FixAction,
    FixPlan,
    InstalledPackage,
    PackageInfo,
    SkipReason,
    UnfixableViolation,
    Violation,
)
from chill_out.registry_client import RegistryClient
from chill_out.state import (
    AvoidingRelease,
    ChillOutState,
    ManagedPin,
    PinMechanism,
    RemovalOutcome,
)


async def check_one(
    pkg: InstalledPackage,
    client: RegistryClient,
    config: ChillOutConfig,
    semaphore: asyncio.Semaphore,
    *,
    fast: bool,
    parser: VersionParser,
    now: pendulum.DateTime,
    on_complete: Callable[[InstalledPackage], None] | None = None,
) -> Violation | SkipReason | None:
    """
    Fetch and evaluate a single package.

    Returns:
        A `Violation` if the package is within its cooldown window,
        a `SkipReason` if the package could not be evaluated,
        or `None` if it has cleared cooldown.

    The `on_complete` callback fires once the package has been evaluated,
    regardless of outcome. Useful for wiring up progress reporting without
    coupling the runner to a particular UI library.
    """
    async with semaphore:
        try:
            try:
                info = await client.fetch_package(pkg.name)
            except RegistryError as exc:
                logger.warning(f"Skipping {pkg.name}: {exc}")
                return SkipReason(package=pkg, reason=str(exc))

            if info is None:
                return SkipReason(package=pkg, reason="not found in registry")

            published = info.published_at(pkg.version)
            if published is None:
                return SkipReason(package=pkg, reason=f"no publish date for {pkg.version}")

            rel_type = release_type(pkg.version, parser)
            violating, age_days, limit_days = is_within_cooldown(published, rel_type, config, now=now)
            if violating:
                safe = None if fast else find_safe_version(pkg.version, info, config, parser, now=now)
                return Violation(
                    package=pkg,
                    release_type=rel_type,
                    age_days=age_days,
                    limit_days=limit_days,
                    published=published,
                    safe_version=safe,
                )
            return None

        finally:
            if on_complete is not None:
                on_complete(pkg)


async def check_async(
    ecosystem: Ecosystem,
    *,
    config: ChillOutConfig | None = None,
    fast: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
    http: httpx.AsyncClient | None = None,
    now: pendulum.DateTime | None = None,
    on_start: Callable[[list[InstalledPackage]], None] | None = None,
    on_progress: Callable[[InstalledPackage], None] | None = None,
) -> CheckReport:
    """
    Run the full cooldown check for the given ecosystem.

    Every package recorded in the project's lockfile is audited, principals and transitives
    alike. The lockfile is the source of truth for what the ecosystem will actually install;
    anything declared in the project's primary manifest but not yet locked is out of scope by
    design.

    Args:
        ecosystem:   The detected or selected ecosystem backend.
        config:      Cooldown configuration. If omitted, it is loaded from the ecosystem's project root.
        fast:        If True, skip the safe-version lookup for faster runs.
        concurrency: Maximum simultaneous registry requests.
        http:        Optional pre-configured HTTP client (mostly useful for testing).
        now:         Override the "now" timestamp used when comparing ages (testing).
        on_start:    Optional callback fired once with the full list of packages about to be checked. Use it to size a
                     progress bar.
        on_progress: Optional callback fired once per package after it has been evaluated. Use it to advance a
                     progress bar.
    """
    config = config or load_config(ecosystem.root, ecosystem.kind)
    now = now or pendulum.now("UTC")
    all_packages = ecosystem.load_installed()
    packages = filter_by_groups(all_packages, config)
    if on_start is not None:
        on_start(list(packages))
    semaphore = asyncio.Semaphore(concurrency)

    own_http = http is None
    if own_http:
        http = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
    http = ChillOutError.enforce_defined(http, "internal error: http client was not initialized")

    try:
        client = RegistryClient(ecosystem, http)
        parser = ecosystem.parse_version
        results = await asyncio.gather(
            *(
                check_one(pkg, client, config, semaphore, fast=fast, parser=parser, now=now, on_complete=on_progress)
                for pkg in packages
            )
        )
    finally:
        if own_http:
            await http.aclose()

    report = CheckReport(ecosystem=ecosystem.kind, checked=list(packages))
    for outcome in results:
        if isinstance(outcome, Violation):
            report.violations.append(outcome)
        elif isinstance(outcome, SkipReason):
            report.skipped.append(outcome)
    return report


def check(
    root: Path,
    *,
    ecosystem_kind: EcosystemKind | None = None,
    config: ChillOutConfig | None = None,
    fast: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> CheckReport:
    """
    Synchronous convenience wrapper around `check_async`.

    Auto-detects the ecosystem from `root` unless `ecosystem_kind` is given.
    """
    ecosystem = get_ecosystem(ecosystem_kind, root) if ecosystem_kind else detect_ecosystem(root)
    return asyncio.run(
        check_async(
            ecosystem,
            config=config,
            fast=fast,
            concurrency=concurrency,
        )
    )


def plan_fixes(report: CheckReport, *, fix_style: FixStyle = FixStyle.EXACT) -> FixPlan:
    """
    Build a basic fix plan from a report, without principal range checking.

    Each violation with a known safe version becomes a single `FixAction` that pins the package
    directly in the project's primary manifest. Transitive violations get pinned as direct deps
    too, so the resolver hoists them and they win over the principal's declared range. Violations
    with no known safe version land in `FixPlan.unfixable` so the caller can report them.

    The `fix_style` parameter controls how each pin is rendered into the
    manifest. See `chill_out.constants.FixStyle`.

    For the smarter version that range-checks transitive pins against the
    installed principal and rolls the principal back when the declared range
    can't admit the safe transitive, use `plan_fixes_async`.
    """
    plan = FixPlan()
    for v in report.violations:
        if v.safe_version is None:
            plan.unfixable.append(UnfixableViolation(v, "no safe version found within the cooldown window"))
            continue
        plan.actions.append(FixAction(package=v.name, version=v.safe_version.version, style=fix_style))
    plan.actions = dedupe_actions(plan.actions)
    return plan


async def plan_fixes_async(
    report: CheckReport,
    ecosystem: Ecosystem,
    *,
    config: ChillOutConfig | None = None,
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
       older principal exists, record the violation in `unfixable` with a
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
    fix_style = config.fix_style
    parser = ecosystem.parse_version

    own_http = http is None
    if own_http:
        http = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
    http = ChillOutError.enforce_defined(http, "internal error: http client was not initialized")
    client = RegistryClient(ecosystem, http)

    installed_by_name: dict[str, InstalledPackage] = {p.name: p for p in report.checked}

    def pin(v: Violation, version: str) -> FixAction:
        """Build the right kind of pin for this violation.

        Shared transitive violations (multiple workspace members pull the
        same install in) need an override-style pin because a member-level
        `dependencies` entry can't dislodge a sibling-shared copy. Direct
        violations on the current project's own manifest stay as plain pins
        even when the package happens to be shared, since the user
        explicitly declared it here.

        Override-bound actions are forced to `FixStyle.EXACT` regardless
        of the configured style, because the entire reason an override
        exists is to dodge a specific just-released version: a range there
        would let the resolver wander right back into the cooldown window.
        """
        use_overrides = v.is_shared and bool(v.via)
        style = FixStyle.EXACT if use_overrides else fix_style
        return FixAction(package=v.name, version=version, via_overrides=use_overrides, style=style)

    plan = FixPlan()
    try:
        for v in report.violations:
            if v.safe_version is None:
                plan.unfixable.append(UnfixableViolation(v, "no safe version found within the cooldown window"))
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
            candidate_versions = candidate_principal_versions(
                principal_info, principal_pkg.version, config, parser, now
            )
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
                parser,
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
            # Principal rollback uses an exact pin regardless of fix_style:
            # a range here would let the resolver pick the latest in-range
            # principal, which could land back on the conflicting version.
            # The paired transitive pin is also exact so the two halves of
            # the rollback can't drift apart on the next install.
            plan.actions.append(FixAction(package=v.via, version=principal_safe.version, style=FixStyle.EXACT))
            plan.actions.append(FixAction(package=v.name, version=v.safe_version.version, style=FixStyle.EXACT))
    finally:
        if own_http:
            await http.aclose()

    plan.actions = dedupe_actions(plan.actions)
    return plan


def candidate_principal_versions(
    info: PackageInfo,
    installed_version: str,
    config: ChillOutConfig,
    parser: VersionParser,
    now: pendulum.DateTime,
) -> list[str]:
    """
    Pick the set of principal versions worth fetching manifests for.

    Strict subset of `find_safe_principal_version`'s candidate filter that
    avoids the manifest fetch (which is only needed for the ones that survive
    the cooldown filter).
    """
    current_v = parser(installed_version)
    if current_v is None:
        return []
    out: list[str] = []
    for ver_str, release in info.releases.items():
        v = parser(ver_str)
        if v is None or v >= current_v or v.is_prerelease:
            continue
        rel_type = release_type(ver_str, parser)
        violating, _, _ = is_within_cooldown(release.published, rel_type, config, now=now)
        if violating:
            continue
        out.append(ver_str)
    return out


def filter_by_groups(packages: list[InstalledPackage], config: ChillOutConfig) -> list[InstalledPackage]:
    """
    Drop installed packages whose semantic groups don't intersect `include_groups`.

    A package with an empty `groups` tuple is treated as "unknown origin"
    and always kept; this preserves the historical behavior for ecosystem
    backends or test fixtures that don't attribute groups. Packages with at
    least one group are kept only when at least one of their groups is in
    the configured set.
    """
    allowed = config.include_group_set
    if not allowed:
        return []
    out: list[InstalledPackage] = []
    for pkg in packages:
        if not pkg.groups:
            out.append(pkg)
            continue
        if any(g in allowed for g in pkg.groups):
            out.append(pkg)
    return out


def dedupe_actions(actions: Iterable[FixAction]) -> list[FixAction]:
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


@dataclass
class CleanupReport:
    """Outcome of `cleanup_managed_pins`.

    Each list holds the `ManagedPin` records the runner attempted to remove during cleanup,
    grouped by the result the ecosystem returned. `removed` entries were successfully cleaned
    out of the manifest. `drifted` entries are still present in the manifest but their value
    differs from what chill-out wrote, so the ecosystem left them alone and the runner has
    dropped them from state. `orphan` entries were no longer in the manifest at all and have
    also been dropped from state.
    """

    removed: list[ManagedPin] = field(default_factory=list)
    drifted: list[ManagedPin] = field(default_factory=list)
    orphan: list[ManagedPin] = field(default_factory=list)


def cleanup_managed_pins(eco: Ecosystem, state: ChillOutState) -> CleanupReport:
    """Walk every pin in `state.managed_pins` and try to remove it from the project's manifests.

    Mutates `state.managed_pins` in place: every entry is dropped regardless of outcome
    (REMOVED, DRIFTED, and ORPHAN all leave nothing for chill-out to track going forward). The
    returned `CleanupReport` lets the caller surface drift warnings to the user without
    re-walking state.

    The ecosystem is responsible for the per-pin manifest edit; this function does not
    regenerate any lockfile. The caller is expected to trigger lockfile regeneration once after
    cleanup so the project is in a consistent state before fresh fixes are applied.
    """
    report = CleanupReport()
    for pin in list(state.managed_pins):
        outcome = eco.remove_managed_pin(pin)
        if outcome is RemovalOutcome.REMOVED:
            report.removed.append(pin)
        elif outcome is RemovalOutcome.DRIFTED:
            report.drifted.append(pin)
        else:
            report.orphan.append(pin)
    state.managed_pins = []
    return report


def build_managed_pins(
    applied: AppliedFixes,
    violations: Iterable[Violation],
    config: ChillOutConfig,
    *,
    now: pendulum.DateTime | None = None,
) -> list[ManagedPin]:
    """Build the `ManagedPin` records that should be saved into state for one fix run.

    Pairs every `AppliedFix` from `applied.entries` with the `Violation` that motivated it
    (matched by package name) so the resulting `AvoidingRelease` snapshot captures why the pin
    exists. Pins for which no matching violation can be found are skipped, since chill-out has
    no avoiding-metadata to attach.

    `config` is consulted to derive the cooldown window for the violation's release type so the
    snapshot reflects the policy in force at the time the pin was written.
    """
    timestamp = now if now is not None else pendulum.now("UTC")
    by_name: dict[str, Violation] = {}
    for v in violations:
        # Multiple violations for the same package would be unusual, but if it ever happens,
        # the first wins; the structured AppliedFix already carries the action that ran.
        by_name.setdefault(v.name, v)

    pins: list[ManagedPin] = []
    for entry in applied.entries:
        violation = by_name.get(entry.action.package)
        if violation is None:
            continue
        cooldown_days = config.for_release_type(violation.release_type)
        pins.append(
            ManagedPin(
                package=entry.action.package,
                ecosystem=violation.package.ecosystem,
                mechanism=PinMechanism.OVERRIDE if entry.via_overrides else PinMechanism.DIRECT,
                manifest_path=entry.manifest_path,
                pinned_spec=entry.pinned_spec,
                applied_at=timestamp,
                avoiding=AvoidingRelease(
                    version=violation.version,
                    release_type=violation.release_type,
                    published_at=violation.published,
                    cooldown_days=cooldown_days,
                ),
            )
        )
    return pins


async def audit_one(
    pin: ManagedPin,
    client: RegistryClient,
    config: ChillOutConfig,
    semaphore: asyncio.Semaphore,
    *,
    now: pendulum.DateTime,
) -> AuditedPin:
    """
    Look up the avoided release's current state for one managed pin.

    The audit is a read-only lookup: it asks the registry whether the
    release the pin is dodging has cleared its cooldown window or been
    pulled outright, and slots the result into one of the four
    `AuditStatus` buckets. The current `config` drives the cooldown
    threshold so the verdict matches what `chill-out fix --cleanup` would
    do on its next run.
    """
    cooldown_days = config.for_release_type(pin.avoiding.release_type)
    async with semaphore:
        try:
            info = await client.fetch_package(pin.package)
        except RegistryError as exc:
            return AuditedPin(
                pin=pin,
                status=AuditStatus.UNKNOWN,
                current_age_days=None,
                cooldown_days=cooldown_days,
                detail=str(exc),
            )

    if info is None:
        return AuditedPin(
            pin=pin,
            status=AuditStatus.UNKNOWN,
            current_age_days=None,
            cooldown_days=cooldown_days,
            detail="package not found in registry",
        )

    release = info.releases.get(pin.avoiding.version)
    if release is None:
        return AuditedPin(
            pin=pin,
            status=AuditStatus.UNKNOWN,
            current_age_days=None,
            cooldown_days=cooldown_days,
            detail=f"version {pin.avoiding.version} not present in registry response",
        )

    age_days = (now - release.published).in_days()

    if release.yanked:
        return AuditedPin(
            pin=pin,
            status=AuditStatus.YANKED,
            current_age_days=age_days,
            cooldown_days=cooldown_days,
        )

    violating, _, _ = is_within_cooldown(release.published, pin.avoiding.release_type, config, now=now)
    status = AuditStatus.FRESH if violating else AuditStatus.STALE
    return AuditedPin(
        pin=pin,
        status=status,
        current_age_days=age_days,
        cooldown_days=cooldown_days,
    )


async def audit_async(
    state: ChillOutState,
    ecosystem: Ecosystem,
    *,
    config: ChillOutConfig | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    http: httpx.AsyncClient | None = None,
    now: pendulum.DateTime | None = None,
) -> AuditReport:
    """
    Audit every managed pin in `state` against the live registry.

    Builds one `AuditedPin` per entry in `state.managed_pins`, preserving
    the state file's order so the resulting report mirrors the user's
    mental model of the file. The lookup is read-only; nothing on disk is
    touched. The caller decides what to do with the result -- typically
    print a summary table and exit with a status code.

    Owns the `httpx.AsyncClient` only when one isn't supplied, mirroring
    `check_async`'s ownership rules.
    """
    if config is None:
        config = load_config(ecosystem.root, ecosystem.kind)
    now = now or pendulum.now("UTC")
    semaphore = asyncio.Semaphore(concurrency)

    own_http = http is None
    http_client = http if http is not None else httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
    try:
        client = RegistryClient(ecosystem, http_client)
        tasks = [audit_one(pin, client, config, semaphore, now=now) for pin in state.managed_pins]
        entries = list(await asyncio.gather(*tasks)) if tasks else []
    finally:
        if own_http:
            await http_client.aclose()

    return AuditReport(ecosystem=ecosystem.kind, entries=entries)

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

from chill_out.config import CooldownConfig, load_config
from chill_out.constants import DEFAULT_CONCURRENCY, DEFAULT_TIMEOUT, EcosystemKind
from chill_out.cooldown import find_safe_version, is_within_cooldown, release_type
from chill_out.ecosystems import detect_ecosystem, get_ecosystem
from chill_out.ecosystems.base import Ecosystem, RegistryClient
from chill_out.exceptions import RegistryError
from chill_out.models import CheckReport, FixAction, InstalledPackage, Violation


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
        client = ecosystem.make_client(http)
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
    Convert each violation that has a known safe version into a :class:`FixAction`.

    Transitive violations are written as overrides; principal violations as
    direct dependency pins.
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

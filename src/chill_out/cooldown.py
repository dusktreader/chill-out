"""
Pure cooldown calculation utilities — no I/O.

These functions operate on already-fetched package data so they're trivial to
test in isolation.
"""

from __future__ import annotations

from collections.abc import Callable

import pendulum
import semver

from chill_out.config import CooldownConfig
from chill_out.constants import ReleaseType
from chill_out.models import PackageInfo, SafeVersion, VersionManifest


def parse_version(version: str) -> semver.Version | None:
    """Try to parse a version string with semver; return None if it doesn't conform."""
    try:
        return semver.Version.parse(version)
    except ValueError:
        return None


def release_type(version: str) -> ReleaseType:
    """
    Classify a version string as a major / minor / patch release.

    A non-semver version returns ``ReleaseType.DEFAULT`` so callers always have a
    threshold to fall back on.
    """
    v = parse_version(version)
    if v is None:
        return ReleaseType.DEFAULT
    if v.minor == 0 and v.patch == 0:
        return ReleaseType.MAJOR
    if v.patch == 0:
        return ReleaseType.MINOR
    return ReleaseType.PATCH


def is_within_cooldown(
    published: pendulum.DateTime,
    rel_type: ReleaseType,
    config: CooldownConfig,
    now: pendulum.DateTime | None = None,
) -> tuple[bool, int, int]:
    """
    Determine whether a release is still inside its cooldown window.

    Returns:
        A tuple of ``(violating, age_days, limit_days)``.
    """
    now = now or pendulum.now("UTC")
    age = now - published
    limit = config.for_release_type(rel_type)
    return age.in_days() < limit, age.in_days(), limit


def find_safe_version(
    current: str,
    info: PackageInfo,
    config: CooldownConfig,
    now: pendulum.DateTime | None = None,
) -> SafeVersion | None:
    """
    Return the newest released version strictly older than ``current`` that has
    cleared its own cooldown window.

    Pre-releases are skipped. Versions that cannot be parsed are ignored.
    """
    now = now or pendulum.now("UTC")
    current_v = parse_version(current)
    if current_v is None:
        return None

    candidates: list[tuple[semver.Version, pendulum.DateTime]] = []
    for ver_str, release in info.releases.items():
        v = parse_version(ver_str)
        if v is None or v >= current_v or v.prerelease:
            continue
        rel_type = release_type(ver_str)
        violating, _, _ = is_within_cooldown(release.published, rel_type, config, now=now)
        if violating:
            continue
        candidates.append((v, release.published))

    if not candidates:
        return None

    best_v, best_published = max(candidates, key=lambda x: x[0])
    age_days = (now - best_published).in_days()
    return SafeVersion(version=str(best_v), age_days=age_days)


def find_safe_principal_version(
    principal_current: str,
    principal_info: PackageInfo,
    principal_manifests: dict[str, VersionManifest],
    transitive_name: str,
    transitive_safe: SafeVersion,
    range_satisfies: Callable[[str, str], bool],
    config: CooldownConfig,
    now: pendulum.DateTime | None = None,
) -> SafeVersion | None:
    """
    Find the newest principal version older than ``principal_current`` that:

    1. Has cleared its own cooldown window.
    2. Declares a range for ``transitive_name`` that is satisfied by
       ``transitive_safe.version`` (so the resolver picks the safe transitive).

    A principal version with no recorded manifest is skipped: if we can't see
    its declared deps we can't be sure the rollback is safe.

    Args:
        principal_current:    The currently installed principal version.
        principal_info:       Release timestamps for the principal package.
        principal_manifests:  Map of ``version_string -> VersionManifest`` for
                              the principal's candidate versions.
        transitive_name:      Name of the transitive dep we're trying to pin.
        transitive_safe:      The safe version we want to pin the transitive to.
        range_satisfies:      Ecosystem-specific range check.
        config:               Cooldown configuration.
        now:                  Override for "now" (used in tests).

    Returns:
        The newest acceptable principal version, or ``None`` if no candidate works.
    """
    now = now or pendulum.now("UTC")
    current_v = parse_version(principal_current)
    if current_v is None:
        return None

    candidates: list[tuple[semver.Version, pendulum.DateTime]] = []
    for ver_str, release in principal_info.releases.items():
        v = parse_version(ver_str)
        if v is None or v >= current_v or v.prerelease:
            continue
        rel_type = release_type(ver_str)
        violating, _, _ = is_within_cooldown(release.published, rel_type, config, now=now)
        if violating:
            continue
        manifest = principal_manifests.get(ver_str)
        if manifest is None:
            continue
        declared_range = manifest.deps.get(transitive_name)
        # If the principal version doesn't declare the transitive at all, it
        # can't pull in the cooldown-violating one, so it's a valid rollback.
        if declared_range is not None and not range_satisfies(transitive_safe.version, declared_range):
            continue
        candidates.append((v, release.published))

    if not candidates:
        return None

    best_v, best_published = max(candidates, key=lambda x: x[0])
    age_days = (now - best_published).in_days()
    return SafeVersion(version=str(best_v), age_days=age_days)

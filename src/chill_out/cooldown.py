"""
Pure cooldown calculation utilities — no I/O.

These functions operate on already-fetched package data so they're trivial to
test in isolation.
"""

from __future__ import annotations

import pendulum
import semver

from chill_out.config import CooldownConfig
from chill_out.constants import BumpType
from chill_out.models import PackageInfo, SafeVersion


def parse_version(version: str) -> semver.Version | None:
    """Try to parse a version string with semver; return None if it doesn't conform."""
    try:
        return semver.Version.parse(version)
    except ValueError:
        return None


def release_type(version: str) -> BumpType:
    """
    Classify a version string as a major / minor / patch release.

    A non-semver version returns ``BumpType.DEFAULT`` so callers always have a
    threshold to fall back on.
    """
    v = parse_version(version)
    if v is None:
        return BumpType.DEFAULT
    if v.minor == 0 and v.patch == 0:
        return BumpType.MAJOR
    if v.patch == 0:
        return BumpType.MINOR
    return BumpType.PATCH


def is_within_cooldown(
    published: pendulum.DateTime,
    bump: BumpType,
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
    limit = config.for_bump(bump)
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
        bump = release_type(ver_str)
        violating, _, _ = is_within_cooldown(release.published, bump, config, now=now)
        if violating:
            continue
        candidates.append((v, release.published))

    if not candidates:
        return None

    best_v, best_published = max(candidates, key=lambda x: x[0])
    age_days = (now - best_published).in_days()
    return SafeVersion(version=str(best_v), age_days=age_days)

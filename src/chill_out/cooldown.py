"""
Pure cooldown calculation utilities — no I/O.

These functions operate on already-fetched package data so they're trivial to
test in isolation. Version parsing is handed in as a callable so the engine
stays ecosystem-agnostic (npm uses semver, pypi uses PEP 440 via
`packaging.Version`, future ecosystems can plug in whatever their registry
publishes).
"""

from collections.abc import Callable

import pendulum

from chill_out.config import ChillOutConfig
from chill_out.constants import ReleaseType
from chill_out.ecosystems.version_parsing import ParsedVersion, VersionParser
from chill_out.models import PackageInfo, SafeVersion, VersionManifest


def release_type(version: str, parser: VersionParser) -> ReleaseType:
    """
    Classify a version string as a major / minor / patch release.

    A version the parser can't make sense of falls through to
    `ReleaseType.DEFAULT` so callers always have a threshold to fall
    back on.
    """
    v = parser(version)
    if v is None:
        return ReleaseType.DEFAULT
    if v.minor == 0 and v.micro == 0:
        return ReleaseType.MAJOR
    if v.micro == 0:
        return ReleaseType.MINOR
    return ReleaseType.PATCH


def is_within_cooldown(
    published: pendulum.DateTime,
    rel_type: ReleaseType,
    config: ChillOutConfig,
    now: pendulum.DateTime | None = None,
) -> tuple[bool, int, int]:
    """
    Determine whether a release is still inside its cooldown window.

    Returns:
        A tuple of `(violating, age_days, limit_days)`.
    """
    now = now or pendulum.now("UTC")
    age = now - published
    limit = config.for_release_type(rel_type)
    return age.in_days() < limit, age.in_days(), limit


def find_safe_version(
    current: str,
    info: PackageInfo,
    config: ChillOutConfig,
    parser: VersionParser,
    now: pendulum.DateTime | None = None,
) -> SafeVersion | None:
    """
    Return the newest released version strictly older than `current` that has
    cleared its own cooldown window.

    Pre-releases are skipped. Versions the parser can't make sense of are
    ignored so a single oddball release in the registry never blocks the
    rest of the search. Yanked releases are skipped too: chill-out is in
    the business of recommending versions to install, and a yanked release
    is one the maintainer has actively withdrawn.
    """
    now = now or pendulum.now("UTC")
    current_v = parser(current)
    if current_v is None:
        return None

    candidates: list[tuple[ParsedVersion, pendulum.DateTime]] = []
    for ver_str, release in info.releases.items():
        v = parser(ver_str)
        if v is None or v >= current_v or v.is_prerelease:
            continue
        if release.yanked:
            continue
        rel_type = release_type(ver_str, parser)
        violating, _, _ = is_within_cooldown(release.published, rel_type, config, now=now)
        if violating:
            continue
        candidates.append((v, release.published))

    if not candidates:
        return None

    best_v, best_published = max(candidates, key=lambda x: x[0])
    age_days = (now - best_published).in_days()
    return SafeVersion(version=best_v.original, age_days=age_days)


def find_safe_principal_version(
    principal_current: str,
    principal_info: PackageInfo,
    principal_manifests: dict[str, VersionManifest],
    transitive_name: str,
    transitive_safe: SafeVersion,
    range_satisfies: Callable[[str, str], bool],
    config: ChillOutConfig,
    parser: VersionParser,
    now: pendulum.DateTime | None = None,
) -> SafeVersion | None:
    """
    Find the newest principal version older than `principal_current` that:

    1. Has cleared its own cooldown window.
    2. Is not yanked.
    3. Declares a range for `transitive_name` that is satisfied by
       `transitive_safe.version` (so the resolver picks the safe transitive).

    A principal version with no recorded manifest is skipped: if we can't see
    its declared deps we can't be sure the rollback is safe.

    Args:
        principal_current:    The currently installed principal version.
        principal_info:       Release timestamps for the principal package.
        principal_manifests:  Map of `version_string -> VersionManifest` for
                              the principal's candidate versions.
        transitive_name:      Name of the transitive dep we're trying to pin.
        transitive_safe:      The safe version we want to pin the transitive to.
        range_satisfies:      Ecosystem-specific range check.
        config:               Cooldown configuration.
        parser:               Ecosystem-specific version parser.
        now:                  Override for "now" (used in tests).

    Returns:
        The newest acceptable principal version, or `None` if no candidate works.
    """
    now = now or pendulum.now("UTC")
    current_v = parser(principal_current)
    if current_v is None:
        return None

    candidates: list[tuple[ParsedVersion, pendulum.DateTime]] = []
    for ver_str, release in principal_info.releases.items():
        v = parser(ver_str)
        if v is None or v >= current_v or v.is_prerelease:
            continue
        if release.yanked:
            continue
        rel_type = release_type(ver_str, parser)
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
    return SafeVersion(version=best_v.original, age_days=age_days)

"""
Basic demos showing chill-out's pure-Python API.

Each function is self-contained and prints to stdout so the demo runner can
display the captured output alongside the source.
"""

from __future__ import annotations

import pendulum
from chill_out import (
    ReleaseType,
    CooldownConfig,
    PackageInfo,
    PackageRelease,
    __version__,
    find_safe_version,
    is_within_cooldown,
    release_type,
)


def demo_01_version() -> None:
    """
    Print the installed `chill-out` version.

    The version string is exposed as a module-level constant so library callers
    can check compatibility without running a subprocess.
    """
    print(f"chill-out {__version__}")


def demo_02_release_type() -> None:
    """
    Classify a version string into a major / minor / patch release.

    `release_type` understands semver and falls back to `ReleaseType.DEFAULT` for
    anything it cannot parse.
    """
    for version in ("2.0.0", "2.1.0", "2.1.3", "garbage"):
        kind = release_type(version)
        print(f"{version:10s} -> {kind.value}")


def demo_03_is_within_cooldown() -> None:
    """
    Check whether a single release is still inside its cooldown window.

    `is_within_cooldown` returns a tuple of `(violating, age_days, limit_days)`
    so callers can render their own messaging.
    """
    config = CooldownConfig(days={ReleaseType.MAJOR: 30, ReleaseType.DEFAULT: 5})
    published = pendulum.now("UTC").subtract(days=2)
    violating, age, limit = is_within_cooldown(published, ReleaseType.MAJOR, config)
    print(f"violating={violating}  age={age}d  limit={limit}d")


def demo_04_find_safe_version() -> None:
    """
    Suggest the newest released version that has cleared its own cooldown.

    The result includes both the version string and how many days it has been
    available so users can judge the rollback risk.
    """
    now = pendulum.now("UTC")
    info = PackageInfo(
        name="example",
        releases={
            "2.0.0": PackageRelease(version="2.0.0", published=now.subtract(days=1)),
            "1.5.0": PackageRelease(version="1.5.0", published=now.subtract(days=60)),
            "1.4.0": PackageRelease(version="1.4.0", published=now.subtract(days=120)),
        },
    )
    config = CooldownConfig(days={ReleaseType.MAJOR: 30, ReleaseType.MINOR: 10, ReleaseType.PATCH: 7, ReleaseType.DEFAULT: 5})
    safe = find_safe_version("2.0.0", info, config)
    if safe is None:
        print("no safe rollback target")
    else:
        print(f"safe rollback: {safe.version} ({safe.age_days}d old)")

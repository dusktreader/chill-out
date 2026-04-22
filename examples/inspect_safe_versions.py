"""
Use the pure cooldown helpers without the orchestrator.

This is useful when you already have package data in memory (for example from
a custom registry, an internal mirror, or a generated SBOM) and just want to
ask "is this version safe to use?" without spinning up `httpx` or subprocesses.
"""

from __future__ import annotations

import pendulum
from chill_out import (
    BumpType,
    CooldownConfig,
    PackageInfo,
    PackageRelease,
    find_safe_version,
    is_within_cooldown,
    release_type,
)


def main() -> None:
    now = pendulum.now("UTC")

    info = PackageInfo(
        name="example",
        releases={
            "2.0.0": PackageRelease(version="2.0.0", published=now.subtract(days=1)),
            "1.5.0": PackageRelease(version="1.5.0", published=now.subtract(days=60)),
            "1.4.0": PackageRelease(version="1.4.0", published=now.subtract(days=120)),
        },
    )
    config = CooldownConfig(days={BumpType.MAJOR: 30, BumpType.MINOR: 10, BumpType.PATCH: 7, BumpType.DEFAULT: 5})

    bump = release_type("2.0.0")
    violating, age, limit = is_within_cooldown(info.published_at("2.0.0"), bump, config)
    print(f"2.0.0 is {bump.value}; violating={violating}, age={age}d, limit={limit}d")

    safe = find_safe_version("2.0.0", info, config)
    if safe:
        print(f"safe rollback: {safe.version} ({safe.age_days}d old)")
    else:
        print("no safe rollback target")


if __name__ == "__main__":
    main()

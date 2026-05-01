"""
Use the pure cooldown helpers without the orchestrator.

This is useful when you already have package data in memory (for example from
a custom registry, an internal mirror, or a generated SBOM) and just want to
ask "is this version safe to use?" without spinning up `httpx` or subprocesses.

The cooldown helpers in `chill_out.cooldown` are ecosystem-agnostic. They take
a `VersionParser` callable so the same engine works for npm's strict semver
and pypi's PEP 440. Each ecosystem ships a parser as `parse_version`, so the
easy path is to instantiate the ecosystem you want and grab that method.
"""

from pathlib import Path

import pendulum
from chill_out import (
    ChillOutConfig,
    PackageInfo,
    PackageRelease,
    PypiEcosystem,
    ReleaseType,
)
from chill_out.cooldown import find_safe_version, is_within_cooldown, release_type

# The version strings below are PEP 440, so plug in pypi's parser. The root
# argument is just there to satisfy the constructor; the parser is a pure
# function and never touches the filesystem.
parser = PypiEcosystem(root=Path(".")).parse_version


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
    config = ChillOutConfig(
        cooldown_days={
            ReleaseType.MAJOR: 30,
            ReleaseType.MINOR: 10,
            ReleaseType.PATCH: 7,
            ReleaseType.DEFAULT: 5,
        }
    )

    rel_type = release_type("2.0.0", parser)
    published = info.published_at("2.0.0")
    assert published is not None, "we just defined this release a few lines up"
    violating, age, limit = is_within_cooldown(published, rel_type, config)
    print(f"2.0.0 is {rel_type.value}; violating={violating}, age={age}d, limit={limit}d")

    safe = find_safe_version("2.0.0", info, config, parser)
    if safe:
        print(f"safe rollback: {safe.version} ({safe.age_days}d old)")
    else:
        print("no safe rollback target")


if __name__ == "__main__":
    main()

"""Unit tests for cooldown calculations."""

from __future__ import annotations

import pendulum
from chill_out.config import CooldownConfig
from chill_out.constants import BumpType
from chill_out.cooldown import find_safe_version, is_within_cooldown, parse_version, release_type
from chill_out.models import PackageInfo, PackageRelease


class TestParseVersion:
    def test_parses_valid_semver(self) -> None:
        v = parse_version("1.2.3")
        assert v is not None
        assert (v.major, v.minor, v.patch) == (1, 2, 3)

    def test_returns_none_for_garbage(self) -> None:
        assert parse_version("not-a-version") is None

    def test_returns_none_for_pep440_style(self) -> None:
        # "1.0" is invalid semver (needs three parts).
        assert parse_version("1.0") is None


class TestReleaseType:
    def test_major(self) -> None:
        assert release_type("2.0.0") is BumpType.MAJOR

    def test_minor(self) -> None:
        assert release_type("2.1.0") is BumpType.MINOR

    def test_patch(self) -> None:
        assert release_type("2.1.3") is BumpType.PATCH

    def test_unknown_falls_back_to_default(self) -> None:
        assert release_type("garbage") is BumpType.DEFAULT


class TestIsWithinCooldown:
    def test_fresh_release_violates(self, fixed_now: pendulum.DateTime) -> None:
        config = CooldownConfig(days={BumpType.MAJOR: 30, BumpType.DEFAULT: 5})
        published = fixed_now.subtract(days=2)
        violating, age, limit = is_within_cooldown(published, BumpType.MAJOR, config, now=fixed_now)
        assert violating is True
        assert age == 2
        assert limit == 30

    def test_old_release_passes(self, fixed_now: pendulum.DateTime) -> None:
        config = CooldownConfig(days={BumpType.PATCH: 7, BumpType.DEFAULT: 5})
        published = fixed_now.subtract(days=30)
        violating, age, limit = is_within_cooldown(published, BumpType.PATCH, config, now=fixed_now)
        assert violating is False
        assert age == 30
        assert limit == 7


class TestFindSafeVersion:
    def _info(self, releases: dict[str, pendulum.DateTime]) -> PackageInfo:
        return PackageInfo(
            name="acme",
            releases={v: PackageRelease(version=v, published=ts) for v, ts in releases.items()},
        )

    def test_returns_newest_safe_older_version(self, fixed_now: pendulum.DateTime) -> None:
        info = self._info(
            {
                "2.0.0": fixed_now.subtract(days=1),  # current, in cooldown
                "1.5.0": fixed_now.subtract(days=60),  # safe, older
                "1.4.0": fixed_now.subtract(days=120),  # safe, but not newest
            }
        )
        config = CooldownConfig(days={BumpType.MAJOR: 30, BumpType.MINOR: 10, BumpType.PATCH: 7, BumpType.DEFAULT: 5})
        safe = find_safe_version("2.0.0", info, config, now=fixed_now)
        assert safe is not None
        assert safe.version == "1.5.0"
        assert safe.age_days == 60

    def test_returns_none_when_only_fresh_alternatives(self, fixed_now: pendulum.DateTime) -> None:
        info = self._info(
            {
                "2.0.0": fixed_now.subtract(days=1),
                "1.5.0": fixed_now.subtract(days=2),  # also too fresh
            }
        )
        config = CooldownConfig(days={BumpType.MINOR: 30, BumpType.MAJOR: 30, BumpType.DEFAULT: 5})
        assert find_safe_version("2.0.0", info, config, now=fixed_now) is None

    def test_skips_prereleases(self, fixed_now: pendulum.DateTime) -> None:
        info = self._info(
            {
                "2.0.0": fixed_now.subtract(days=1),
                "2.0.0-rc1": fixed_now.subtract(days=120),
                "1.0.0": fixed_now.subtract(days=200),
            }
        )
        config = CooldownConfig(days={BumpType.MAJOR: 30, BumpType.MINOR: 30, BumpType.PATCH: 30, BumpType.DEFAULT: 5})
        safe = find_safe_version("2.0.0", info, config, now=fixed_now)
        assert safe is not None
        assert safe.version == "1.0.0"

    def test_returns_none_for_unparsable_current(self, fixed_now: pendulum.DateTime) -> None:
        info = self._info({"1.0.0": fixed_now.subtract(days=200)})
        assert find_safe_version("not-a-version", info, CooldownConfig(), now=fixed_now) is None

    def test_ignores_unparsable_releases(self, fixed_now: pendulum.DateTime) -> None:
        info = self._info(
            {
                "2.0.0": fixed_now.subtract(days=1),
                "junk": fixed_now.subtract(days=200),
                "1.5.0": fixed_now.subtract(days=200),
            }
        )
        config = CooldownConfig()
        safe = find_safe_version("2.0.0", info, config, now=fixed_now)
        assert safe is not None
        assert safe.version == "1.5.0"

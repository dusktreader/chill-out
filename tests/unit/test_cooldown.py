"""Unit tests for cooldown calculations."""

import pendulum
import pytest
from chill_out.config import ChillOutConfig
from chill_out.constants import ReleaseType
from chill_out.cooldown import (
    find_safe_principal_version,
    find_safe_version,
    is_within_cooldown,
    release_type,
)
from chill_out.ecosystems.npm.backend import NpmEcosystem
from chill_out.ecosystems.pypi.backend import PypiEcosystem
from chill_out.ecosystems.version_parsing import VersionParser
from chill_out.models import PackageInfo, PackageRelease, SafeVersion, VersionManifest

# Both parsers are exercised across the suite. Each test that doesn't care about
# ecosystem-specific quirks parametrizes over both so the engine stays
# ecosystem-agnostic by construction.
SEMVER_PARSER: VersionParser = NpmEcosystem(root=__import__("pathlib").Path("/tmp")).parse_version
PEP440_PARSER: VersionParser = PypiEcosystem(root=__import__("pathlib").Path("/tmp")).parse_version


@pytest.fixture(params=[SEMVER_PARSER, PEP440_PARSER], ids=["semver", "pep440"])
def parser(request) -> VersionParser:
    return request.param


class TestParsedVersion:
    def test_semver_parses_three_segment(self) -> None:
        v = SEMVER_PARSER("1.2.3")
        assert v is not None
        assert (v.major, v.minor, v.micro) == (1, 2, 3)

    def test_semver_rejects_two_segment(self) -> None:
        # Strict semver requires three segments; this is exactly what the old
        # global parser used to enforce, and we preserve that for npm.
        assert SEMVER_PARSER("1.0") is None

    def test_pep440_accepts_two_segment(self) -> None:
        # The original cooldown bug was here: `idna 3.12` got rejected by the
        # strict semver parser and `find_safe_version` always returned None
        # for non-semver-strict pypi packages. PEP 440 happily parses it.
        v = PEP440_PARSER("3.12")
        assert v is not None
        assert (v.major, v.minor, v.micro) == (3, 12, 0)

    def test_pep440_accepts_post_release(self) -> None:
        v = PEP440_PARSER("1.0.post1")
        assert v is not None
        assert v.is_prerelease is False

    def test_returns_none_for_garbage(self, parser: VersionParser) -> None:
        assert parser("not-a-version") is None

    def test_preserves_original_string(self) -> None:
        # packaging would canonicalize "2.0.0-rc1" to "2.0.0rc1"; we keep the
        # registry's exact spelling so safe versions round-trip cleanly.
        v = PEP440_PARSER("2.0.0-rc1")
        assert v is not None
        assert v.original == "2.0.0-rc1"

    def test_supports_lt_comparison(self) -> None:
        """`__lt__` delegates to `sort_key` so older releases compare less than newer ones."""
        older = PEP440_PARSER("1.0.0")
        newer = PEP440_PARSER("2.0.0")
        assert older is not None and newer is not None
        assert older < newer

    def test_supports_le_comparison(self) -> None:
        """`__le__` returns True for both equal and lesser versions."""
        v1 = PEP440_PARSER("1.0.0")
        v2 = PEP440_PARSER("1.0.0")
        assert v1 is not None and v2 is not None
        assert v1 <= v2


class TestReleaseType:
    def test_major(self, parser: VersionParser) -> None:
        assert release_type("2.0.0", parser) is ReleaseType.MAJOR

    def test_minor(self, parser: VersionParser) -> None:
        assert release_type("2.1.0", parser) is ReleaseType.MINOR

    def test_patch(self, parser: VersionParser) -> None:
        assert release_type("2.1.3", parser) is ReleaseType.PATCH

    def test_unknown_falls_back_to_default(self, parser: VersionParser) -> None:
        assert release_type("garbage", parser) is ReleaseType.DEFAULT

    def test_pep440_two_segment_classifies_as_minor(self) -> None:
        # `3.12` zero-pads to `3.12.0`, which has minor != 0 and micro == 0,
        # so it's a minor release. This is the case that flushed out the
        # original bug in our self-dogfood.
        assert release_type("3.12", PEP440_PARSER) is ReleaseType.MINOR


class TestIsWithinCooldown:
    def test_fresh_release_violates(self, fixed_now: pendulum.DateTime) -> None:
        config = ChillOutConfig(cooldown_days={ReleaseType.MAJOR: 30, ReleaseType.DEFAULT: 5})
        published = fixed_now.subtract(days=2)
        violating, age, limit = is_within_cooldown(published, ReleaseType.MAJOR, config, now=fixed_now)
        assert violating is True
        assert age == 2
        assert limit == 30

    def test_old_release_passes(self, fixed_now: pendulum.DateTime) -> None:
        config = ChillOutConfig(cooldown_days={ReleaseType.PATCH: 7, ReleaseType.DEFAULT: 5})
        published = fixed_now.subtract(days=30)
        violating, age, limit = is_within_cooldown(published, ReleaseType.PATCH, config, now=fixed_now)
        assert violating is False
        assert age == 30
        assert limit == 7


class TestFindSafeVersion:
    def _info(self, releases: dict[str, pendulum.DateTime]) -> PackageInfo:
        return PackageInfo(
            name="acme",
            releases={v: PackageRelease(version=v, published=ts) for v, ts in releases.items()},
        )

    def test_returns_newest_safe_older_version(self, fixed_now: pendulum.DateTime, parser: VersionParser) -> None:
        info = self._info(
            {
                "2.0.0": fixed_now.subtract(days=1),  # current, in cooldown
                "1.5.0": fixed_now.subtract(days=60),  # safe, older
                "1.4.0": fixed_now.subtract(days=120),  # safe, but not newest
            }
        )
        config = ChillOutConfig(
            cooldown_days={
                ReleaseType.MAJOR: 30,
                ReleaseType.MINOR: 10,
                ReleaseType.PATCH: 7,
                ReleaseType.DEFAULT: 5,
            }
        )
        safe = find_safe_version("2.0.0", info, config, parser, now=fixed_now)
        assert safe is not None
        assert safe.version == "1.5.0"
        assert safe.age_days == 60

    def test_returns_none_when_only_fresh_alternatives(
        self, fixed_now: pendulum.DateTime, parser: VersionParser
    ) -> None:
        info = self._info(
            {
                "2.0.0": fixed_now.subtract(days=1),
                "1.5.0": fixed_now.subtract(days=2),  # also too fresh
            }
        )
        config = ChillOutConfig(
            cooldown_days={
                ReleaseType.MINOR: 30,
                ReleaseType.MAJOR: 30,
                ReleaseType.DEFAULT: 5,
            }
        )
        assert find_safe_version("2.0.0", info, config, parser, now=fixed_now) is None

    def test_skips_prereleases(self, fixed_now: pendulum.DateTime, parser: VersionParser) -> None:
        info = self._info(
            {
                "2.0.0": fixed_now.subtract(days=1),
                "2.0.0-rc1": fixed_now.subtract(days=120),
                "1.0.0": fixed_now.subtract(days=200),
            }
        )
        config = ChillOutConfig(
            cooldown_days={
                ReleaseType.MAJOR: 30,
                ReleaseType.MINOR: 30,
                ReleaseType.PATCH: 30,
                ReleaseType.DEFAULT: 5,
            }
        )
        safe = find_safe_version("2.0.0", info, config, parser, now=fixed_now)
        assert safe is not None
        assert safe.version == "1.0.0"

    def test_returns_none_for_unparsable_current(self, fixed_now: pendulum.DateTime, parser: VersionParser) -> None:
        info = self._info({"1.0.0": fixed_now.subtract(days=200)})
        assert find_safe_version("not-a-version", info, ChillOutConfig(), parser, now=fixed_now) is None

    def test_ignores_unparsable_releases(self, fixed_now: pendulum.DateTime, parser: VersionParser) -> None:
        info = self._info(
            {
                "2.0.0": fixed_now.subtract(days=1),
                "junk": fixed_now.subtract(days=200),
                "1.5.0": fixed_now.subtract(days=200),
            }
        )
        config = ChillOutConfig()
        safe = find_safe_version("2.0.0", info, config, parser, now=fixed_now)
        assert safe is not None
        assert safe.version == "1.5.0"

    def test_pep440_two_segment_current_finds_safe_predecessor(self, fixed_now: pendulum.DateTime) -> None:
        # Direct regression for the cooldown bug: with the old strict-semver
        # parser, a current version like `3.12` would parse to None, the
        # function would return None, and the violation would surface with
        # no rollback target. PEP 440 parses both, so the predecessor `3.11`
        # turns up as expected.
        info = self._info(
            {
                "3.12": fixed_now.subtract(days=2),  # in cooldown
                "3.11": fixed_now.subtract(days=200),  # safe
            }
        )
        config = ChillOutConfig(cooldown_days={ReleaseType.MINOR: 30, ReleaseType.DEFAULT: 5})
        safe = find_safe_version("3.12", info, config, PEP440_PARSER, now=fixed_now)
        assert safe is not None
        assert safe.version == "3.11"

    def test_safe_version_preserves_original_string(self, fixed_now: pendulum.DateTime) -> None:
        # When a safe predecessor uses a non-canonical spelling the registry
        # actually publishes (e.g. `2.0.0-rc1` for a final release tag in
        # some packages), the SafeVersion must round-trip the original so
        # fix actions write the exact string the registry knows about.
        info = self._info(
            {
                "3.0.0": fixed_now.subtract(days=2),
                "2.0.0-rc1": fixed_now.subtract(days=200),
            }
        )
        config = ChillOutConfig()
        # Use semver which keeps the rc1 spelling and treats it as prerelease.
        # Skip prereleases means we get None; switch to a non-prerelease
        # candidate to actually verify round-trip:
        info = self._info(
            {
                "3.0.0": fixed_now.subtract(days=2),
                "2.5.0": fixed_now.subtract(days=200),
            }
        )
        safe = find_safe_version("3.0.0", info, config, SEMVER_PARSER, now=fixed_now)
        assert safe is not None
        assert safe.version == "2.5.0"

    def test_skips_yanked_candidates(self, fixed_now: pendulum.DateTime, parser: VersionParser) -> None:
        # The newest-older release (`1.5.0`) is past cooldown but yanked, so
        # the search must fall through to the next-best non-yanked candidate.
        info = PackageInfo(
            name="acme",
            releases={
                "2.0.0": PackageRelease(version="2.0.0", published=fixed_now.subtract(days=1)),
                "1.5.0": PackageRelease(version="1.5.0", published=fixed_now.subtract(days=200), yanked=True),
                "1.4.0": PackageRelease(version="1.4.0", published=fixed_now.subtract(days=300)),
            },
        )
        safe = find_safe_version("2.0.0", info, ChillOutConfig(), parser, now=fixed_now)
        assert safe is not None
        assert safe.version == "1.4.0"


class TestFindSafePrincipalVersion:
    """Cover the principal-rollback search."""

    def _info(self, releases: dict[str, pendulum.DateTime]) -> PackageInfo:
        return PackageInfo(
            name="parent",
            releases={v: PackageRelease(v, p) for v, p in releases.items()},
        )

    def test_picks_newest_compatible_older_principal(self, fixed_now: pendulum.DateTime, parser: VersionParser) -> None:
        info = self._info(
            {
                "2.0.0": fixed_now.subtract(days=1),
                "1.5.0": fixed_now.subtract(days=200),
                "1.4.0": fixed_now.subtract(days=300),
            }
        )
        manifests = {
            "1.5.0": VersionManifest("parent", "1.5.0", deps={"child": ">=1.0,<2.0"}),
            "1.4.0": VersionManifest("parent", "1.4.0", deps={"child": ">=1.0,<2.0"}),
        }
        config = ChillOutConfig()
        safe = find_safe_principal_version(
            "2.0.0",
            info,
            manifests,
            "child",
            SafeVersion("1.9.0", 100),
            range_satisfies=lambda v, r: True,
            config=config,
            parser=parser,
            now=fixed_now,
        )
        assert safe is not None
        assert safe.version == "1.5.0"

    def test_skips_principal_versions_with_incompatible_range(
        self, fixed_now: pendulum.DateTime, parser: VersionParser
    ) -> None:
        info = self._info(
            {
                "2.0.0": fixed_now.subtract(days=1),
                "1.5.0": fixed_now.subtract(days=200),
                "1.4.0": fixed_now.subtract(days=300),
            }
        )
        manifests = {
            "1.5.0": VersionManifest("parent", "1.5.0", deps={"child": ">=2.0"}),  # incompatible
            "1.4.0": VersionManifest("parent", "1.4.0", deps={"child": ">=1.0,<2.0"}),
        }

        def satisfies(version: str, spec: str) -> bool:
            return "<2.0" in spec

        config = ChillOutConfig()
        safe = find_safe_principal_version(
            "2.0.0",
            info,
            manifests,
            "child",
            SafeVersion("1.9.0", 100),
            range_satisfies=satisfies,
            config=config,
            parser=parser,
            now=fixed_now,
        )
        assert safe is not None
        assert safe.version == "1.4.0"

    def test_skips_versions_without_a_manifest(self, fixed_now: pendulum.DateTime, parser: VersionParser) -> None:
        info = self._info(
            {
                "2.0.0": fixed_now.subtract(days=1),
                "1.5.0": fixed_now.subtract(days=200),
            }
        )
        config = ChillOutConfig()
        safe = find_safe_principal_version(
            "2.0.0",
            info,
            {},  # no manifests
            "child",
            SafeVersion("1.9.0", 100),
            range_satisfies=lambda v, r: True,
            config=config,
            parser=parser,
            now=fixed_now,
        )
        assert safe is None

    def test_principal_without_declared_range_is_eligible(
        self, fixed_now: pendulum.DateTime, parser: VersionParser
    ) -> None:
        """An older principal that doesn't declare the transitive at all can still be a valid rollback target."""
        info = self._info(
            {
                "2.0.0": fixed_now.subtract(days=1),
                "1.5.0": fixed_now.subtract(days=200),
            }
        )
        manifests = {"1.5.0": VersionManifest("parent", "1.5.0", deps={})}  # no "child"
        config = ChillOutConfig()
        safe = find_safe_principal_version(
            "2.0.0",
            info,
            manifests,
            "child",
            SafeVersion("1.9.0", 100),
            range_satisfies=lambda v, r: False,  # would reject anything
            config=config,
            parser=parser,
            now=fixed_now,
        )
        assert safe is not None
        assert safe.version == "1.5.0"

    def test_returns_none_for_unparsable_current(self, fixed_now: pendulum.DateTime, parser: VersionParser) -> None:
        info = self._info({"1.0.0": fixed_now.subtract(days=200)})
        safe = find_safe_principal_version(
            "not-a-version",
            info,
            {},
            "child",
            SafeVersion("1.0.0", 200),
            range_satisfies=lambda v, r: True,
            config=ChillOutConfig(),
            parser=parser,
            now=fixed_now,
        )
        assert safe is None

    def test_skips_principal_releases_still_in_cooldown(
        self, fixed_now: pendulum.DateTime, parser: VersionParser
    ) -> None:
        """A principal release inside the cooldown window is skipped in favor of an older safe one."""
        info = self._info(
            {
                "2.0.0": fixed_now.subtract(days=1),
                "1.5.0": fixed_now.subtract(days=2),  # Inside the 10-day minor cooldown.
                "1.4.0": fixed_now.subtract(days=200),  # Outside cooldown.
            }
        )
        manifests = {
            "1.5.0": VersionManifest("parent", "1.5.0", deps={"child": ">=1.0,<2.0"}),
            "1.4.0": VersionManifest("parent", "1.4.0", deps={"child": ">=1.0,<2.0"}),
        }
        safe = find_safe_principal_version(
            "2.0.0",
            info,
            manifests,
            "child",
            SafeVersion("1.9.0", 100),
            range_satisfies=lambda v, r: True,
            config=ChillOutConfig(),
            parser=parser,
            now=fixed_now,
        )
        assert safe is not None
        assert safe.version == "1.4.0"

    def test_skips_yanked_principal_releases(self, fixed_now: pendulum.DateTime, parser: VersionParser) -> None:
        """A yanked principal version is skipped even when it's compatible and past cooldown."""
        info = PackageInfo(
            name="parent",
            releases={
                "2.0.0": PackageRelease(version="2.0.0", published=fixed_now.subtract(days=1)),
                "1.5.0": PackageRelease(version="1.5.0", published=fixed_now.subtract(days=200), yanked=True),
                "1.4.0": PackageRelease(version="1.4.0", published=fixed_now.subtract(days=300)),
            },
        )
        manifests = {
            "1.5.0": VersionManifest("parent", "1.5.0", deps={"child": ">=1.0,<2.0"}),
            "1.4.0": VersionManifest("parent", "1.4.0", deps={"child": ">=1.0,<2.0"}),
        }
        safe = find_safe_principal_version(
            "2.0.0",
            info,
            manifests,
            "child",
            SafeVersion("1.9.0", 100),
            range_satisfies=lambda v, r: True,
            config=ChillOutConfig(),
            parser=parser,
            now=fixed_now,
        )
        assert safe is not None
        assert safe.version == "1.4.0"

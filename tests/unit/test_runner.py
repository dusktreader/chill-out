"""Unit tests for the runner orchestration."""

from __future__ import annotations

import httpx
import pendulum
import pytest
from chill_out.config import CooldownConfig
from chill_out.constants import BumpType, EcosystemKind
from chill_out.ecosystems.base import Ecosystem, RegistryClient
from chill_out.models import FixAction, InstalledPackage, PackageInfo, PackageRelease, VersionManifest
from chill_out.runner import _dedupe_actions, check_async, plan_fixes


class _FakeClient(RegistryClient):
    def __init__(
        self,
        http: httpx.AsyncClient,
        data: dict[str, PackageInfo | None],
        manifests: dict[tuple[str, str], VersionManifest | None] | None = None,
    ) -> None:
        super().__init__(http)
        self.data = data
        self.manifests = manifests or {}
        self.calls: list[str] = []

    async def fetch_package(self, name: str) -> PackageInfo | None:
        self.calls.append(name)
        return self.data.get(name)

    async def fetch_version_manifest(self, name: str, version: str) -> VersionManifest | None:
        return self.manifests.get((name, version))


class _FakeEcosystem(Ecosystem):
    kind = EcosystemKind.NPM

    def __init__(self, packages, data, manifests=None) -> None:
        super().__init__(root=__import__("pathlib").Path("/tmp"))
        self.packages = packages
        self.data = data
        self.manifests = manifests or {}

    @classmethod
    def detect(cls, root) -> bool:
        return False

    def load_installed(self, *, deep: bool = False) -> list[InstalledPackage]:
        return list(self.packages)

    def make_client(self, http: httpx.AsyncClient) -> RegistryClient:
        return _FakeClient(http, self.data, self.manifests)

    def apply_fixes(self, actions):
        return [f"applied {a.package}={a.version}" for a in actions]

    def range_satisfies(self, version: str, range_spec: str) -> bool:
        # Permissive default for tests; specific tests can override.
        return True


@pytest.fixture
def now() -> pendulum.DateTime:
    return pendulum.datetime(2026, 1, 1, tz="UTC")


@pytest.fixture
def config() -> CooldownConfig:
    return CooldownConfig(days={BumpType.MAJOR: 30, BumpType.MINOR: 10, BumpType.PATCH: 7, BumpType.DEFAULT: 5})


class TestCheckAsync:
    async def test_no_violations(self, now, config) -> None:
        eco = _FakeEcosystem(
            packages=[InstalledPackage(name="ok", version="1.0.0", ecosystem=EcosystemKind.NPM)],
            data={
                "ok": PackageInfo(
                    name="ok",
                    releases={"1.0.0": PackageRelease(version="1.0.0", published=now.subtract(days=200))},
                )
            },
        )
        report = await check_async(eco, config=config, now=now)
        assert report.violations == []
        assert report.skipped == []

    async def test_detects_violation_with_safe_version(self, now, config) -> None:
        eco = _FakeEcosystem(
            packages=[InstalledPackage(name="x", version="2.0.0", ecosystem=EcosystemKind.NPM)],
            data={
                "x": PackageInfo(
                    name="x",
                    releases={
                        "2.0.0": PackageRelease(version="2.0.0", published=now.subtract(days=2)),
                        "1.5.0": PackageRelease(version="1.5.0", published=now.subtract(days=200)),
                    },
                )
            },
        )
        report = await check_async(eco, config=config, now=now)
        assert len(report.violations) == 1
        v = report.violations[0]
        assert v.name == "x"
        assert v.safe_version is not None
        assert v.safe_version.version == "1.5.0"

    async def test_fast_skips_safe_version_lookup(self, now, config) -> None:
        eco = _FakeEcosystem(
            packages=[InstalledPackage(name="x", version="2.0.0", ecosystem=EcosystemKind.NPM)],
            data={
                "x": PackageInfo(
                    name="x",
                    releases={
                        "2.0.0": PackageRelease(version="2.0.0", published=now.subtract(days=2)),
                        "1.5.0": PackageRelease(version="1.5.0", published=now.subtract(days=200)),
                    },
                )
            },
        )
        report = await check_async(eco, config=config, fast=True, now=now)
        assert report.violations[0].safe_version is None

    async def test_skips_packages_missing_from_registry(self, now, config) -> None:
        eco = _FakeEcosystem(
            packages=[InstalledPackage(name="ghost", version="1.0.0", ecosystem=EcosystemKind.NPM)],
            data={"ghost": None},
        )
        report = await check_async(eco, config=config, now=now)
        assert report.violations == []
        assert len(report.skipped) == 1
        assert "not found" in report.skipped[0][1]

    async def test_skips_when_no_publish_date(self, now, config) -> None:
        eco = _FakeEcosystem(
            packages=[InstalledPackage(name="x", version="9.9.9", ecosystem=EcosystemKind.NPM)],
            data={
                "x": PackageInfo(
                    name="x",
                    releases={"1.0.0": PackageRelease(version="1.0.0", published=now.subtract(days=200))},
                )
            },
        )
        report = await check_async(eco, config=config, now=now)
        assert report.violations == []
        assert "no publish date" in report.skipped[0][1]


class TestPlanFixes:
    def test_principal_violation_becomes_dependency_pin(self, now, config) -> None:
        from chill_out.models import CheckReport, SafeVersion, Violation

        v = Violation(
            package=InstalledPackage(name="x", version="2.0.0", ecosystem=EcosystemKind.NPM),
            bump=BumpType.MAJOR,
            age_days=2,
            limit_days=30,
            published=now.subtract(days=2),
            safe_version=SafeVersion(version="1.5.0", age_days=200),
        )
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[v.package], violations=[v])
        actions = plan_fixes(report)
        assert actions == [FixAction(package="x", version="1.5.0", is_override=False)]

    def test_transitive_violation_becomes_override(self, now, config) -> None:
        from chill_out.models import CheckReport, SafeVersion, Violation

        v = Violation(
            package=InstalledPackage(name="t", version="2.0.0", ecosystem=EcosystemKind.NPM, via_chain=("principal",)),
            bump=BumpType.MAJOR,
            age_days=2,
            limit_days=30,
            published=now.subtract(days=2),
            safe_version=SafeVersion(version="1.5.0", age_days=200),
        )
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[v.package], violations=[v])
        actions = plan_fixes(report)
        assert actions[0].is_override is True

    def test_skips_violations_without_safe_version(self, now, config) -> None:
        from chill_out.models import CheckReport, Violation

        v = Violation(
            package=InstalledPackage(name="x", version="1.0.0", ecosystem=EcosystemKind.NPM),
            bump=BumpType.MAJOR,
            age_days=2,
            limit_days=30,
            published=now.subtract(days=2),
            safe_version=None,
        )
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[v.package], violations=[v])
        assert plan_fixes(report) == []


class TestDedupe:
    def test_keeps_smallest_version(self) -> None:
        actions = [
            FixAction(package="a", version="2.0.0"),
            FixAction(package="a", version="1.5.0"),
            FixAction(package="a", version="1.7.0"),
        ]
        out = _dedupe_actions(actions)
        assert len(out) == 1
        assert out[0].version == "1.5.0"

    def test_treats_workspaces_separately(self) -> None:
        actions = [
            FixAction(package="a", version="1.0.0", workspace="w1"),
            FixAction(package="a", version="2.0.0", workspace="w2"),
        ]
        out = _dedupe_actions(actions)
        assert len(out) == 2


class TestPlanFixesAsync:
    """Cover the principal-rollback path in plan_fixes_async."""

    def _violation(
        self,
        name: str,
        version: str,
        safe: str,
        via: str | None,
        principal_version: str | None = None,
    ):
        from chill_out.models import SafeVersion

        via_chain = (via,) if via else ()
        installed = InstalledPackage(name=name, version=version, ecosystem=EcosystemKind.NPM, via_chain=via_chain)
        from chill_out.models import Violation

        return Violation(
            package=installed,
            bump=BumpType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.datetime(2025, 12, 30, tz="UTC"),
            safe_version=SafeVersion(safe, 100),
        )

    @pytest.mark.asyncio
    async def test_principal_violation_emits_install(self, now: pendulum.DateTime, config) -> None:
        from chill_out.models import CheckReport
        from chill_out.runner import plan_fixes_async

        v = self._violation("requests", "2.31.0", "2.30.0", via=None)
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[v.package], violations=[v])
        eco = _FakeEcosystem(packages=[v.package], data={}, manifests={})
        actions = await plan_fixes_async(report, eco, config=config, now=now)
        assert len(actions) == 1
        assert actions[0].package == "requests"
        assert actions[0].version == "2.30.0"
        assert actions[0].is_override is False

    @pytest.mark.asyncio
    async def test_transitive_with_compatible_principal_emits_override_only(
        self, now: pendulum.DateTime, config
    ) -> None:
        from chill_out.models import CheckReport, VersionManifest
        from chill_out.runner import plan_fixes_async

        principal = InstalledPackage(name="parent", version="1.0.0", ecosystem=EcosystemKind.NPM)
        v = self._violation("child", "2.0.0", "1.9.0", via="parent")
        report = CheckReport(
            ecosystem=EcosystemKind.NPM,
            checked=[v.package, principal],
            violations=[v],
        )
        manifests = {
            ("parent", "1.0.0"): VersionManifest("parent", "1.0.0", deps={"child": ">=1.0"}),
        }
        eco = _FakeEcosystem(packages=[v.package, principal], data={}, manifests=manifests)
        # Default range_satisfies in _FakeEcosystem returns True.
        actions = await plan_fixes_async(report, eco, config=config, now=now)
        assert len(actions) == 1
        assert actions[0].package == "child"
        assert actions[0].is_override is True

    @pytest.mark.asyncio
    async def test_incompatible_principal_triggers_rollback(self, now: pendulum.DateTime, config) -> None:
        from chill_out.models import CheckReport, VersionManifest
        from chill_out.runner import plan_fixes_async

        principal = InstalledPackage(name="parent", version="2.0.0", ecosystem=EcosystemKind.NPM)
        v = self._violation("child", "2.0.0", "1.9.0", via="parent")

        # Principal info: 2.0.0 (installed, in cooldown), 1.5.0 (out of cooldown)
        parent_info = PackageInfo(
            name="parent",
            releases={
                "2.0.0": PackageRelease("2.0.0", now.subtract(days=1)),
                "1.5.0": PackageRelease("1.5.0", now.subtract(days=200)),
            },
        )
        manifests = {
            # Installed 2.0.0 declares range that EXCLUDES the safe child.
            ("parent", "2.0.0"): VersionManifest("parent", "2.0.0", deps={"child": ">=2.0"}),
            # 1.5.0 declares a range that ADMITS the safe child.
            ("parent", "1.5.0"): VersionManifest("parent", "1.5.0", deps={"child": ">=1.0,<2.0"}),
        }

        class _RollbackEcosystem(_FakeEcosystem):
            def range_satisfies(self, version, range_spec):
                return "<2.0" in range_spec

        report = CheckReport(
            ecosystem=EcosystemKind.NPM,
            checked=[v.package, principal],
            violations=[v],
        )
        eco = _RollbackEcosystem(
            packages=[v.package, principal],
            data={"parent": parent_info},
            manifests=manifests,
        )
        actions = await plan_fixes_async(report, eco, config=config, now=now)
        # Two actions: principal install + child override.
        assert len(actions) == 2
        by_name = {a.package: a for a in actions}
        assert by_name["parent"].version == "1.5.0"
        assert by_name["parent"].is_override is False
        assert by_name["child"].version == "1.9.0"
        assert by_name["child"].is_override is True

    @pytest.mark.asyncio
    async def test_no_compatible_principal_skips_violation(self, now: pendulum.DateTime, config) -> None:
        from chill_out.models import CheckReport, VersionManifest
        from chill_out.runner import plan_fixes_async

        principal = InstalledPackage(name="parent", version="2.0.0", ecosystem=EcosystemKind.NPM)
        v = self._violation("child", "2.0.0", "1.9.0", via="parent")
        parent_info = PackageInfo(
            name="parent",
            releases={
                "2.0.0": PackageRelease("2.0.0", now.subtract(days=1)),
                "1.5.0": PackageRelease("1.5.0", now.subtract(days=200)),
            },
        )
        manifests = {
            ("parent", "2.0.0"): VersionManifest("parent", "2.0.0", deps={"child": ">=2.0"}),
            ("parent", "1.5.0"): VersionManifest("parent", "1.5.0", deps={"child": ">=2.0"}),
        }

        class _StrictEcosystem(_FakeEcosystem):
            def range_satisfies(self, version, range_spec):
                return False  # nothing matches

        report = CheckReport(
            ecosystem=EcosystemKind.NPM,
            checked=[v.package, principal],
            violations=[v],
        )
        eco = _StrictEcosystem(
            packages=[v.package, principal],
            data={"parent": parent_info},
            manifests=manifests,
        )
        actions = await plan_fixes_async(report, eco, config=config, now=now)
        assert actions == []

    @pytest.mark.asyncio
    async def test_unknown_principal_falls_back_to_override(self, now: pendulum.DateTime, config) -> None:
        from chill_out.models import CheckReport
        from chill_out.runner import plan_fixes_async

        # Transitive violation where the principal isn't in report.checked.
        v = self._violation("child", "2.0.0", "1.9.0", via="ghost")
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[v.package], violations=[v])
        eco = _FakeEcosystem(packages=[v.package], data={}, manifests={})
        actions = await plan_fixes_async(report, eco, config=config, now=now)
        assert len(actions) == 1
        assert actions[0].is_override is True

    @pytest.mark.asyncio
    async def test_skips_violation_with_no_safe_version(self, now: pendulum.DateTime, config) -> None:
        from chill_out.models import CheckReport, Violation
        from chill_out.runner import plan_fixes_async

        installed = InstalledPackage(name="x", version="1.0", ecosystem=EcosystemKind.NPM)
        v = Violation(
            package=installed,
            bump=BumpType.MAJOR,
            age_days=2,
            limit_days=30,
            published=now.subtract(days=2),
            safe_version=None,
        )
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[installed], violations=[v])
        eco = _FakeEcosystem(packages=[installed], data={}, manifests={})
        actions = await plan_fixes_async(report, eco, config=config, now=now)
        assert actions == []

"""Unit tests for the runner orchestration."""

from __future__ import annotations

import httpx
import pendulum
import pytest

from chill_out.config import CooldownConfig
from chill_out.constants import BumpType, EcosystemKind
from chill_out.ecosystems.base import Ecosystem, RegistryClient
from chill_out.models import FixAction, InstalledPackage, PackageInfo, PackageRelease
from chill_out.runner import _dedupe_actions, check_async, plan_fixes


class _FakeClient(RegistryClient):
    def __init__(self, http: httpx.AsyncClient, data: dict[str, PackageInfo | None]) -> None:
        super().__init__(http)
        self.data = data
        self.calls: list[str] = []

    async def fetch_package(self, name: str) -> PackageInfo | None:
        self.calls.append(name)
        return self.data.get(name)


class _FakeEcosystem(Ecosystem):
    kind = EcosystemKind.NPM

    def __init__(self, packages, data) -> None:
        super().__init__(root=__import__("pathlib").Path("/tmp"))
        self.packages = packages
        self.data = data

    @classmethod
    def detect(cls, root) -> bool:
        return False

    def load_installed(self, *, deep: bool = False) -> list[InstalledPackage]:
        return list(self.packages)

    def make_client(self, http: httpx.AsyncClient) -> RegistryClient:
        return _FakeClient(http, self.data)

    def apply_fixes(self, actions):
        return [f"applied {a.package}={a.version}" for a in actions]


@pytest.fixture
def now() -> pendulum.DateTime:
    return pendulum.datetime(2026, 1, 1, tz="UTC")


@pytest.fixture
def config() -> CooldownConfig:
    return CooldownConfig(
        days={BumpType.MAJOR: 30, BumpType.MINOR: 10, BumpType.PATCH: 7, BumpType.DEFAULT: 5}
    )


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
            package=InstalledPackage(
                name="t", version="2.0.0", ecosystem=EcosystemKind.NPM, via_chain=("principal",)
            ),
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

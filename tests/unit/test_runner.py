"""Unit tests for the runner orchestration."""

from pathlib import Path

import httpx
import pendulum
import pytest
from chill_out.config import ChillOutConfig
from chill_out.constants import EcosystemKind, ReleaseType
from chill_out.ecosystems.backend import Ecosystem
from chill_out.models import FixAction, InstalledPackage, PackageInfo, PackageRelease, VersionManifest, Violation
from chill_out.runner import check_async, dedupe_actions, plan_fixes
from chill_out.state import ManagedPin


class _FakeEcosystem(Ecosystem):
    kind = EcosystemKind.NPM

    def __init__(self, packages, data, manifests=None) -> None:
        self.root = __import__("pathlib").Path("/tmp")
        self.packages = packages
        self.data = data
        self.manifests = manifests or {}
        self.calls: list[str] = []

    def load_installed(self) -> list[InstalledPackage]:
        return list(self.packages)

    async def fetch_package(self, name: str, http: httpx.AsyncClient) -> PackageInfo | None:
        self.calls.append(name)
        return self.data.get(name)

    async def fetch_version_manifest(self, name: str, version: str, http: httpx.AsyncClient) -> VersionManifest | None:
        return self.manifests.get((name, version))

    def apply_fixes(self, actions):
        from chill_out.models import AppliedFix, AppliedFixes

        entries = [
            AppliedFix(action=a, pinned_spec=a.version, via_overrides=False, manifest_path=Path("pyproject.toml"))
            for a in actions
        ]
        log = [f"applied {a.package}={a.version}" for a in actions]
        return AppliedFixes(entries=entries, log=log)

    def remove_managed_pin(self, pin):
        from chill_out.state import RemovalOutcome

        return RemovalOutcome.ORPHAN

    def regenerate_lockfile(self) -> str:
        return "ran: fake-regen"

    def range_satisfies(self, version: str, range_spec: str) -> bool:
        # Permissive default for tests; specific tests can override.
        return True

    def parse_version(self, version: str):
        # Tests use semver-shaped strings throughout; reuse the real npm parser
        # so the engine sees ecosystem-faithful ParsedVersion objects.
        from chill_out.ecosystems.npm.backend import NpmEcosystem

        return NpmEcosystem(root=self.root).parse_version(version)

    def supports_overrides(self) -> bool:
        return False

    def apply_override_fixes(self, actions):
        return None

    def workspace_topology(self):
        return None


@pytest.fixture
def now() -> pendulum.DateTime:
    return pendulum.datetime(2026, 1, 1, tz="UTC")


@pytest.fixture
def config() -> ChillOutConfig:
    return ChillOutConfig(
        cooldown_days={ReleaseType.MAJOR: 30, ReleaseType.MINOR: 10, ReleaseType.PATCH: 7, ReleaseType.DEFAULT: 5}
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
        assert report.skipped[0].package.name == "ghost"
        assert "not found" in report.skipped[0].reason

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
        assert report.skipped[0].package.name == "x"
        assert "no publish date" in report.skipped[0].reason

    async def test_progress_callbacks_fire(self, now, config) -> None:
        packages = [
            InstalledPackage(name="a", version="1.0.0", ecosystem=EcosystemKind.NPM),
            InstalledPackage(name="b", version="2.0.0", ecosystem=EcosystemKind.NPM),
        ]
        eco = _FakeEcosystem(
            packages=packages,
            data={
                "a": PackageInfo(
                    name="a",
                    releases={"1.0.0": PackageRelease(version="1.0.0", published=now.subtract(days=200))},
                ),
                "b": PackageInfo(
                    name="b",
                    releases={"2.0.0": PackageRelease(version="2.0.0", published=now.subtract(days=200))},
                ),
            },
        )
        started: list[list[InstalledPackage]] = []
        progressed: list[InstalledPackage] = []
        await check_async(
            eco,
            config=config,
            now=now,
            on_start=started.append,
            on_progress=progressed.append,
        )
        assert len(started) == 1
        assert {p.name for p in started[0]} == {"a", "b"}
        assert {p.name for p in progressed} == {"a", "b"}

    async def test_progress_callback_fires_for_skipped_packages(self, now, config) -> None:
        # The callback should fire even when the package is skipped (not found,
        # no publish date, etc) so the progress bar always reaches 100%.
        eco = _FakeEcosystem(
            packages=[InstalledPackage(name="ghost", version="1.0.0", ecosystem=EcosystemKind.NPM)],
            data={"ghost": None},
        )
        progressed: list[InstalledPackage] = []
        await check_async(eco, config=config, now=now, on_progress=progressed.append)
        assert len(progressed) == 1
        assert progressed[0].name == "ghost"


class TestPlanFixes:
    def test_principal_violation_becomes_dependency_pin(self, now, config) -> None:
        from chill_out.models import CheckReport, SafeVersion, Violation

        v = Violation(
            package=InstalledPackage(name="x", version="2.0.0", ecosystem=EcosystemKind.NPM),
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=now.subtract(days=2),
            safe_version=SafeVersion(version="1.5.0", age_days=200),
        )
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[v.package], violations=[v])
        plan = plan_fixes(report)
        assert plan.actions == [FixAction(package="x", version="1.5.0")]
        assert plan.unfixable == []

    def test_transitive_violation_becomes_direct_pin(self, now, config) -> None:
        from chill_out.models import CheckReport, SafeVersion, Violation

        v = Violation(
            package=InstalledPackage(name="t", version="2.0.0", ecosystem=EcosystemKind.NPM, via_chain=("principal",)),
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=now.subtract(days=2),
            safe_version=SafeVersion(version="1.5.0", age_days=200),
        )
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[v.package], violations=[v])
        plan = plan_fixes(report)
        assert plan.actions == [FixAction(package="t", version="1.5.0")]
        assert plan.unfixable == []

    def test_records_unfixable_when_no_safe_version(self, now, config) -> None:
        from chill_out.models import CheckReport, Violation

        v = Violation(
            package=InstalledPackage(name="x", version="1.0.0", ecosystem=EcosystemKind.NPM),
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=now.subtract(days=2),
            safe_version=None,
        )
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[v.package], violations=[v])
        plan = plan_fixes(report)
        assert plan.actions == []
        assert len(plan.unfixable) == 1
        assert plan.unfixable[0].violation is v
        assert "no safe version" in plan.unfixable[0].reason


class TestDedupe:
    def test_keeps_smallest_version(self) -> None:
        actions = [
            FixAction(package="a", version="2.0.0"),
            FixAction(package="a", version="1.5.0"),
            FixAction(package="a", version="1.7.0"),
        ]
        out = dedupe_actions(actions)
        assert len(out) == 1
        assert out[0].version == "1.5.0"

    def test_treats_workspaces_separately(self) -> None:
        # Workspace support was removed in v1; ensure dedupe collapses by package name only.
        actions = [
            FixAction(package="a", version="1.0.0"),
            FixAction(package="a", version="2.0.0"),
        ]
        out = dedupe_actions(actions)
        assert len(out) == 1
        assert out[0].version == "1.0.0"


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
            release_type=ReleaseType.MAJOR,
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
        plan = await plan_fixes_async(report, eco, config=config, now=now)
        assert len(plan.actions) == 1
        assert plan.actions[0].package == "requests"
        assert plan.actions[0].version == "2.30.0"
        assert plan.unfixable == []

    @pytest.mark.asyncio
    async def test_transitive_with_compatible_principal_emits_direct_pin(self, now: pendulum.DateTime, config) -> None:
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
        plan = await plan_fixes_async(report, eco, config=config, now=now)
        assert len(plan.actions) == 1
        assert plan.actions[0].package == "child"
        assert plan.actions[0].version == "1.9.0"
        assert plan.unfixable == []

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
        plan = await plan_fixes_async(report, eco, config=config, now=now)
        # Two actions: principal rollback + child direct pin.
        assert len(plan.actions) == 2
        by_name = {a.package: a for a in plan.actions}
        assert by_name["parent"].version == "1.5.0"
        assert by_name["child"].version == "1.9.0"
        assert plan.unfixable == []

    @pytest.mark.asyncio
    async def test_no_compatible_principal_records_unfixable(self, now: pendulum.DateTime, config) -> None:
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
        plan = await plan_fixes_async(report, eco, config=config, now=now)
        assert plan.actions == []
        assert len(plan.unfixable) == 1
        reason = plan.unfixable[0].reason
        assert "conflicts with parent@2.0.0" in reason
        assert "downgrade parent manually" in reason

    @pytest.mark.asyncio
    async def test_unknown_principal_falls_back_to_direct_pin(self, now: pendulum.DateTime, config) -> None:
        from chill_out.models import CheckReport
        from chill_out.runner import plan_fixes_async

        # Transitive violation where the principal isn't in report.checked.
        v = self._violation("child", "2.0.0", "1.9.0", via="ghost")
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[v.package], violations=[v])
        eco = _FakeEcosystem(packages=[v.package], data={}, manifests={})
        plan = await plan_fixes_async(report, eco, config=config, now=now)
        assert len(plan.actions) == 1
        assert plan.actions[0].package == "child"
        assert plan.actions[0].version == "1.9.0"
        assert plan.unfixable == []

    @pytest.mark.asyncio
    async def test_records_unfixable_when_no_safe_version(self, now: pendulum.DateTime, config) -> None:
        from chill_out.models import CheckReport, Violation
        from chill_out.runner import plan_fixes_async

        installed = InstalledPackage(name="x", version="1.0", ecosystem=EcosystemKind.NPM)
        v = Violation(
            package=installed,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=now.subtract(days=2),
            safe_version=None,
        )
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[installed], violations=[v])
        eco = _FakeEcosystem(packages=[installed], data={}, manifests={})
        plan = await plan_fixes_async(report, eco, config=config, now=now)
        assert plan.actions == []
        assert len(plan.unfixable) == 1
        assert "no safe version" in plan.unfixable[0].reason

    @pytest.mark.asyncio
    async def test_intermediate_ancestor_range_triggers_rollback(self, now: pendulum.DateTime, config) -> None:
        """A conflicting range deep in the chain (not the principal) should still trigger rollback.

        Regression for the bug where the runner only checked the principal's
        declared range. When `principal -> middle -> child` and `middle`
        is the layer that excludes the safe `child`, the old code would
        see no conflict at the principal level (which doesn't even mention
        `child`) and pin directly. The fix walks every ancestor.
        """
        from chill_out.models import CheckReport, SafeVersion, VersionManifest, Violation
        from chill_out.runner import plan_fixes_async

        # Three-deep chain: principal -> middle -> child.
        principal = InstalledPackage(name="principal", version="2.0.0", ecosystem=EcosystemKind.NPM)
        middle = InstalledPackage(
            name="middle",
            version="2.0.0",
            ecosystem=EcosystemKind.NPM,
            via_chain=("principal",),
        )
        child_pkg = InstalledPackage(
            name="child",
            version="2.0.0",
            ecosystem=EcosystemKind.NPM,
            # via_chain[0] is the immediate parent (middle), [-1] is the principal.
            via_chain=("middle", "principal"),
        )
        v = Violation(
            package=child_pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=now.subtract(days=2),
            safe_version=SafeVersion("1.9.0", 100),
        )

        principal_info = PackageInfo(
            name="principal",
            releases={
                "2.0.0": PackageRelease("2.0.0", now.subtract(days=1)),
                "1.5.0": PackageRelease("1.5.0", now.subtract(days=200)),
            },
        )
        manifests = {
            # Principal doesn't declare child at all.
            ("principal", "2.0.0"): VersionManifest("principal", "2.0.0", deps={"middle": ">=2.0"}),
            ("principal", "1.5.0"): VersionManifest("principal", "1.5.0", deps={"middle": ">=1.0,<2.0"}),
            # Middle is the one that excludes the safe child.
            ("middle", "2.0.0"): VersionManifest("middle", "2.0.0", deps={"child": ">=2.0"}),
        }

        class _SemverEcosystem(_FakeEcosystem):
            def range_satisfies(self, version, range_spec):
                # Only ranges containing "<2.0" admit "1.9.0".
                return "<2.0" in range_spec

        report = CheckReport(
            ecosystem=EcosystemKind.NPM,
            checked=[principal, middle, child_pkg],
            violations=[v],
        )
        eco = _SemverEcosystem(
            packages=[principal, middle, child_pkg],
            data={"principal": principal_info},
            manifests=manifests,
        )
        plan = await plan_fixes_async(report, eco, config=config, now=now)
        # Should detect the middle-layer conflict and roll the principal back.
        by_name = {a.package: a for a in plan.actions}
        assert "principal" in by_name, f"expected principal rollback, got {plan.actions}"
        assert by_name["principal"].version == "1.5.0"
        assert by_name["child"].version == "1.9.0"


class TestPlanFixesViaOverridesRouting:
    """Tier 2: shared transitive violations should set FixAction.via_overrides."""

    @pytest.mark.asyncio
    async def test_shared_transitive_routes_through_overrides(self, now: pendulum.DateTime, config) -> None:
        from chill_out.models import CheckReport, SafeVersion, VersionManifest, Violation
        from chill_out.runner import plan_fixes_async

        # A transitive child shared by two workspace members
        principal = InstalledPackage(name="parent", version="1.0.0", ecosystem=EcosystemKind.NPM)
        child_pkg = InstalledPackage(
            name="child",
            version="2.0.0",
            ecosystem=EcosystemKind.NPM,
            via_chain=("parent",),
            member_owners=("api", "backend"),
        )
        v = Violation(
            package=child_pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.datetime(2025, 12, 30, tz="UTC"),
            safe_version=SafeVersion("1.9.0", 100),
        )
        manifests = {
            ("parent", "1.0.0"): VersionManifest("parent", "1.0.0", deps={"child": ">=1.0"}),
        }
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[child_pkg, principal], violations=[v])
        eco = _FakeEcosystem(packages=[child_pkg, principal], data={}, manifests=manifests)
        plan = await plan_fixes_async(report, eco, config=config, now=now)
        assert len(plan.actions) == 1
        assert plan.actions[0].package == "child"
        assert plan.actions[0].via_overrides is True

    @pytest.mark.asyncio
    async def test_unshared_transitive_stays_direct(self, now: pendulum.DateTime, config) -> None:
        from chill_out.models import CheckReport, SafeVersion, VersionManifest, Violation
        from chill_out.runner import plan_fixes_async

        principal = InstalledPackage(name="parent", version="1.0.0", ecosystem=EcosystemKind.NPM)
        child_pkg = InstalledPackage(
            name="child",
            version="2.0.0",
            ecosystem=EcosystemKind.NPM,
            via_chain=("parent",),
            member_owners=("api",),  # only one member
        )
        v = Violation(
            package=child_pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.datetime(2025, 12, 30, tz="UTC"),
            safe_version=SafeVersion("1.9.0", 100),
        )
        manifests = {
            ("parent", "1.0.0"): VersionManifest("parent", "1.0.0", deps={"child": ">=1.0"}),
        }
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[child_pkg, principal], violations=[v])
        eco = _FakeEcosystem(packages=[child_pkg, principal], data={}, manifests=manifests)
        plan = await plan_fixes_async(report, eco, config=config, now=now)
        assert len(plan.actions) == 1
        assert plan.actions[0].via_overrides is False

    @pytest.mark.asyncio
    async def test_shared_direct_violation_stays_direct(self, now: pendulum.DateTime, config) -> None:
        # Direct (non-via) violations don't go through overrides even if shared
        # because they live in the project's own manifest.
        from chill_out.models import CheckReport, SafeVersion, Violation
        from chill_out.runner import plan_fixes_async

        pkg = InstalledPackage(
            name="requests",
            version="2.31.0",
            ecosystem=EcosystemKind.PYPI,
            member_owners=("api", "backend"),
        )
        v = Violation(
            package=pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.datetime(2025, 12, 30, tz="UTC"),
            safe_version=SafeVersion("2.30.0", 100),
        )
        report = CheckReport(ecosystem=EcosystemKind.PYPI, checked=[pkg], violations=[v])
        eco = _FakeEcosystem(packages=[pkg], data={}, manifests={})
        plan = await plan_fixes_async(report, eco, config=config, now=now)
        assert len(plan.actions) == 1
        assert plan.actions[0].via_overrides is False


class TestFilterByGroups:
    """Runner filters out installed packages whose groups don't intersect include_groups."""

    def test_keeps_packages_in_allowed_groups(self) -> None:
        from chill_out.constants import DependencyGroup
        from chill_out.runner import filter_by_groups

        cfg = ChillOutConfig(include_groups=(DependencyGroup.MAIN,))
        pkgs = [
            InstalledPackage(name="a", version="1", ecosystem=EcosystemKind.NPM, groups=(DependencyGroup.MAIN,)),
            InstalledPackage(name="b", version="2", ecosystem=EcosystemKind.NPM, groups=(DependencyGroup.DEV,)),
            InstalledPackage(
                name="c", version="3", ecosystem=EcosystemKind.NPM, groups=(DependencyGroup.MAIN, DependencyGroup.DEV)
            ),
        ]
        kept = {p.name for p in filter_by_groups(pkgs, cfg)}
        # `b` is dev-only and gets dropped; `c` is kept because it intersects MAIN.
        assert kept == {"a", "c"}

    def test_keeps_packages_with_no_attributed_groups(self) -> None:
        from chill_out.runner import filter_by_groups

        # Empty `groups` means "ecosystem didn't attribute" -- conservatively kept
        # so older callers and test fixtures keep working.
        cfg = ChillOutConfig()
        pkgs = [InstalledPackage(name="legacy", version="1", ecosystem=EcosystemKind.NPM)]
        assert filter_by_groups(pkgs, cfg) == pkgs

    def test_empty_include_groups_drops_everything(self) -> None:
        from chill_out.constants import DependencyGroup
        from chill_out.runner import filter_by_groups

        cfg = ChillOutConfig(include_groups=())
        pkgs = [
            InstalledPackage(name="a", version="1", ecosystem=EcosystemKind.NPM, groups=(DependencyGroup.MAIN,)),
            InstalledPackage(name="b", version="2", ecosystem=EcosystemKind.NPM),
        ]
        assert filter_by_groups(pkgs, cfg) == []

    async def test_check_async_skips_filtered_packages_entirely(self, now) -> None:
        """Filtered packages don't even get a registry call; they vanish from the report."""
        from chill_out.constants import DependencyGroup

        cfg = ChillOutConfig(
            cooldown_days={ReleaseType.MAJOR: 30, ReleaseType.DEFAULT: 5},
            include_groups=(DependencyGroup.MAIN,),
        )
        eco = _FakeEcosystem(
            packages=[
                InstalledPackage(
                    name="prod-pkg", version="1.0.0", ecosystem=EcosystemKind.NPM, groups=(DependencyGroup.MAIN,)
                ),
                InstalledPackage(
                    name="dev-pkg", version="1.0.0", ecosystem=EcosystemKind.NPM, groups=(DependencyGroup.DEV,)
                ),
            ],
            data={
                "prod-pkg": PackageInfo(
                    name="prod-pkg",
                    releases={"1.0.0": PackageRelease(version="1.0.0", published=now.subtract(days=200))},
                ),
                "dev-pkg": PackageInfo(
                    name="dev-pkg",
                    releases={"1.0.0": PackageRelease(version="1.0.0", published=now.subtract(days=200))},
                ),
            },
        )
        report = await check_async(eco, config=cfg, now=now)
        names_checked = {p.name for p in report.checked}
        # Only the MAIN package is in the checked set; dev-pkg never made it past the filter.
        assert names_checked == {"prod-pkg"}


class TestPlanFixesStylePlumbing:
    """`plan_fixes` and `plan_fixes_async` thread style through correctly.

    These exercise the planning layer only; rendering of the final spec
    string is covered by the per-ecosystem tests.
    """

    def _violation(self, now, *, via_chain=(), member_owners=()):
        from chill_out.models import InstalledPackage, SafeVersion, Violation

        return Violation(
            package=InstalledPackage(
                name="x",
                version="2.0.0",
                ecosystem=EcosystemKind.NPM,
                via_chain=via_chain,
                member_owners=member_owners,
            ),
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=now.subtract(days=2),
            safe_version=SafeVersion(version="1.5.0", age_days=200),
        )

    def test_plan_fixes_threads_explicit_style(self, now) -> None:
        from chill_out.constants import FixStyle
        from chill_out.models import CheckReport
        from chill_out.runner import plan_fixes

        v = self._violation(now)
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[v.package], violations=[v])
        plan = plan_fixes(report, fix_style=FixStyle.COMPATIBLE)
        assert len(plan.actions) == 1
        assert plan.actions[0].style is FixStyle.COMPATIBLE

    def test_plan_fixes_defaults_to_exact(self, now) -> None:
        from chill_out.constants import FixStyle
        from chill_out.models import CheckReport
        from chill_out.runner import plan_fixes

        v = self._violation(now)
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[v.package], violations=[v])
        plan = plan_fixes(report)
        assert plan.actions[0].style is FixStyle.EXACT

    async def test_plan_fixes_async_uses_config_style_for_simple_pin(self, now, config) -> None:
        from chill_out.constants import FixStyle
        from chill_out.models import CheckReport
        from chill_out.runner import plan_fixes_async

        v = self._violation(now)
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[v.package], violations=[v])
        cfg = config.model_copy(update={"fix_style": FixStyle.COMPATIBLE})
        eco = _FakeEcosystem(packages=[v.package], data={}, manifests={})
        plan = await plan_fixes_async(report, eco, config=cfg, now=now)
        assert plan.actions[0].style is FixStyle.COMPATIBLE
        assert plan.actions[0].via_overrides is False

    async def test_plan_fixes_async_forces_exact_for_override_actions(self, now, config) -> None:
        from chill_out.constants import FixStyle
        from chill_out.models import CheckReport
        from chill_out.runner import plan_fixes_async

        # Shared transitive: more than one workspace member owner triggers
        # `is_shared`, and `via_chain` makes it transitive. Together
        # those are what flip the planner into the override path.
        v = self._violation(now, via_chain=("principal",), member_owners=("a", "b"))
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[v.package], violations=[v])
        cfg = config.model_copy(update={"fix_style": FixStyle.COMPATIBLE})
        # Principal isn't in the installed set, so plan_fixes_async takes
        # the early-out branch that pins the transitive directly via the
        # `pin()` helper; the override path is selected purely by
        # is_shared+via, and our config asks for COMPATIBLE which the
        # helper must downgrade to EXACT for safety.
        eco = _FakeEcosystem(packages=[v.package], data={}, manifests={})
        plan = await plan_fixes_async(report, eco, config=cfg, now=now)
        assert plan.actions[0].via_overrides is True
        assert plan.actions[0].style is FixStyle.EXACT


class TestCleanupManagedPins:
    """`cleanup_managed_pins` walks state, calls remove_managed_pin per entry, returns categorized report."""

    def _pin(self, package: str, mechanism_value: str = "direct") -> ManagedPin:
        from chill_out.state import AvoidingRelease, PinMechanism

        return ManagedPin(
            package=package,
            ecosystem=EcosystemKind.NPM,
            mechanism=PinMechanism(mechanism_value),
            manifest_path=Path("package.json"),
            pinned_spec=f"{package}==1.0.0",
            applied_at=pendulum.datetime(2026, 1, 1, tz="UTC"),
            avoiding=AvoidingRelease(
                version="2.0.0",
                release_type=ReleaseType.MAJOR,
                published_at=pendulum.datetime(2026, 1, 1, tz="UTC"),
                cooldown_days=30,
            ),
        )

    def test_categorizes_outcomes_and_clears_state(self) -> None:
        from chill_out.runner import cleanup_managed_pins
        from chill_out.state import ChillOutState, RemovalOutcome

        pin_removed = self._pin("a")
        pin_drifted = self._pin("b")
        pin_orphan = self._pin("c")
        state = ChillOutState.empty()
        state.managed_pins.extend([pin_removed, pin_drifted, pin_orphan])

        outcomes = {
            "a": RemovalOutcome.REMOVED,
            "b": RemovalOutcome.DRIFTED,
            "c": RemovalOutcome.ORPHAN,
        }

        class _Eco(_FakeEcosystem):
            def remove_managed_pin(self, pin):
                return outcomes[pin.package]

        eco = _Eco(packages=[], data={})
        report = cleanup_managed_pins(eco, state)
        assert [p.package for p in report.removed] == ["a"]
        assert [p.package for p in report.drifted] == ["b"]
        assert [p.package for p in report.orphan] == ["c"]
        # Every pin is dropped from state regardless of outcome.
        assert state.managed_pins == []

    def test_empty_state_is_a_no_op(self) -> None:
        from chill_out.runner import cleanup_managed_pins
        from chill_out.state import ChillOutState

        state = ChillOutState.empty()
        eco = _FakeEcosystem(packages=[], data={})
        report = cleanup_managed_pins(eco, state)
        assert report.removed == []
        assert report.drifted == []
        assert report.orphan == []


class TestBuildManagedPins:
    """`build_managed_pins` pairs AppliedFix entries with their motivating Violations."""

    def _pkg(self) -> InstalledPackage:
        return InstalledPackage(name="lodash", version="4.17.21", ecosystem=EcosystemKind.NPM)

    def _violation(self, now: pendulum.DateTime, pkg: InstalledPackage) -> Violation:
        from chill_out.models import SafeVersion

        return Violation(
            package=pkg,
            release_type=ReleaseType.MINOR,
            age_days=2,
            limit_days=10,
            published=now,
            safe_version=SafeVersion(version="4.17.20", age_days=200),
        )

    def test_builds_pin_per_matched_fix(self, now, config) -> None:
        from chill_out.models import AppliedFix, AppliedFixes, FixAction
        from chill_out.runner import build_managed_pins
        from chill_out.state import PinMechanism

        pkg = self._pkg()
        violation = self._violation(now, pkg)
        action = FixAction(package="lodash", version="4.17.20")
        applied = AppliedFixes(
            entries=[
                AppliedFix(
                    action=action, pinned_spec="4.17.20", via_overrides=False, manifest_path=Path("package.json")
                )
            ],
            log=["pinned lodash"],
        )
        pins = build_managed_pins(applied, [violation], config, now=now)
        assert len(pins) == 1
        pin = pins[0]
        assert pin.package == "lodash"
        assert pin.ecosystem is EcosystemKind.NPM
        assert pin.mechanism is PinMechanism.DIRECT
        assert pin.manifest_path == Path("package.json")
        assert pin.pinned_spec == "4.17.20"
        assert pin.applied_at == now
        assert pin.avoiding.version == "4.17.21"
        assert pin.avoiding.release_type is ReleaseType.MINOR
        assert pin.avoiding.cooldown_days == 10  # config minor threshold

    def test_marks_override_mechanism(self, now, config) -> None:
        from chill_out.models import AppliedFix, AppliedFixes, FixAction
        from chill_out.runner import build_managed_pins
        from chill_out.state import PinMechanism

        pkg = self._pkg()
        violation = self._violation(now, pkg)
        action = FixAction(package="lodash", version="4.17.20", via_overrides=True)
        applied = AppliedFixes(
            entries=[
                AppliedFix(action=action, pinned_spec="4.17.20", via_overrides=True, manifest_path=Path("package.json"))
            ],
            log=[],
        )
        pins = build_managed_pins(applied, [violation], config, now=now)
        assert pins[0].mechanism is PinMechanism.OVERRIDE

    def test_skips_fixes_with_no_matching_violation(self, now, config) -> None:
        from chill_out.models import AppliedFix, AppliedFixes, FixAction
        from chill_out.runner import build_managed_pins

        action = FixAction(package="orphan-pkg", version="1.0.0")
        applied = AppliedFixes(
            entries=[
                AppliedFix(action=action, pinned_spec="1.0.0", via_overrides=False, manifest_path=Path("package.json"))
            ],
            log=[],
        )
        pins = build_managed_pins(applied, [], config, now=now)
        assert pins == []


class TestAuditAsync:
    """`audit_async` walks the state file and classifies each pin against the live registry."""

    def _pin(
        self,
        package: str,
        avoiding_version: str = "2.0.0",
        published: pendulum.DateTime | None = None,
        cooldown_days: int = 30,
    ) -> ManagedPin:
        from chill_out.state import AvoidingRelease, PinMechanism

        return ManagedPin(
            package=package,
            ecosystem=EcosystemKind.NPM,
            mechanism=PinMechanism.DIRECT,
            manifest_path=Path("package.json"),
            pinned_spec=f"{package}==1.0.0",
            applied_at=pendulum.datetime(2026, 1, 1, tz="UTC"),
            avoiding=AvoidingRelease(
                version=avoiding_version,
                release_type=ReleaseType.MAJOR,
                published_at=published or pendulum.datetime(2026, 1, 1, tz="UTC"),
                cooldown_days=cooldown_days,
            ),
        )

    async def test_buckets_stale_fresh_yanked_unknown(self, now, config) -> None:
        from chill_out.constants import AuditStatus
        from chill_out.runner import audit_async
        from chill_out.state import ChillOutState

        # `now` is 2026-01-01. Major cooldown is 30 days.
        # `stale-pkg` avoided release is 100d old (long past cooldown -> stale).
        # `fresh-pkg` avoided release is 5d old (still in cooldown -> fresh).
        # `yanked-pkg` avoided release is 200d old but yanked (-> yanked).
        # `unknown-pkg` is missing from the registry entirely (-> unknown).
        eco = _FakeEcosystem(
            packages=[],
            data={
                "stale-pkg": PackageInfo(
                    name="stale-pkg",
                    releases={"2.0.0": PackageRelease(version="2.0.0", published=now.subtract(days=100))},
                ),
                "fresh-pkg": PackageInfo(
                    name="fresh-pkg",
                    releases={"2.0.0": PackageRelease(version="2.0.0", published=now.subtract(days=5))},
                ),
                "yanked-pkg": PackageInfo(
                    name="yanked-pkg",
                    releases={"2.0.0": PackageRelease(version="2.0.0", published=now.subtract(days=200), yanked=True)},
                ),
                # `unknown-pkg` deliberately omitted.
            },
        )
        state = ChillOutState.empty()
        state.managed_pins.extend(
            [
                self._pin("stale-pkg"),
                self._pin("fresh-pkg"),
                self._pin("yanked-pkg"),
                self._pin("unknown-pkg"),
            ]
        )
        report = await audit_async(state, eco, config=config, now=now)

        statuses = {e.pin.package: e.status for e in report.entries}
        assert statuses == {
            "stale-pkg": AuditStatus.STALE,
            "fresh-pkg": AuditStatus.FRESH,
            "yanked-pkg": AuditStatus.YANKED,
            "unknown-pkg": AuditStatus.UNKNOWN,
        }
        assert report.has_actionable is True  # stale + yanked
        # Order is preserved from state.
        assert [e.pin.package for e in report.entries] == ["stale-pkg", "fresh-pkg", "yanked-pkg", "unknown-pkg"]

    async def test_unknown_when_avoided_version_missing_from_response(self, now, config) -> None:
        from chill_out.constants import AuditStatus
        from chill_out.runner import audit_async
        from chill_out.state import ChillOutState

        # Registry knows the package but no longer carries the avoided version
        # (could happen if someone hard-deleted it admin-side).
        eco = _FakeEcosystem(
            packages=[],
            data={
                "ghost": PackageInfo(
                    name="ghost",
                    releases={"3.0.0": PackageRelease(version="3.0.0", published=now.subtract(days=200))},
                ),
            },
        )
        state = ChillOutState.empty()
        state.managed_pins.append(self._pin("ghost", avoiding_version="2.0.0"))
        report = await audit_async(state, eco, config=config, now=now)
        assert len(report.entries) == 1
        entry = report.entries[0]
        assert entry.status is AuditStatus.UNKNOWN
        assert entry.detail is not None
        assert "2.0.0" in entry.detail
        assert report.has_actionable is False

    async def test_unknown_when_registry_raises(self, now, config) -> None:
        from chill_out.constants import AuditStatus
        from chill_out.exceptions import RegistryError
        from chill_out.runner import audit_async
        from chill_out.state import ChillOutState

        class _BoomEco(_FakeEcosystem):
            async def fetch_package(self, name: str, http: httpx.AsyncClient) -> PackageInfo | None:
                raise RegistryError("registry exploded")

        eco = _BoomEco(packages=[], data={})
        state = ChillOutState.empty()
        state.managed_pins.append(self._pin("anything"))
        report = await audit_async(state, eco, config=config, now=now)
        assert len(report.entries) == 1
        entry = report.entries[0]
        assert entry.status is AuditStatus.UNKNOWN
        assert "registry exploded" in (entry.detail or "")

    async def test_empty_state_returns_empty_report(self, now, config) -> None:
        from chill_out.runner import audit_async
        from chill_out.state import ChillOutState

        eco = _FakeEcosystem(packages=[], data={})
        report = await audit_async(ChillOutState.empty(), eco, config=config, now=now)
        assert report.entries == []
        assert report.has_actionable is False

    async def test_owns_http_client_when_not_supplied(self, now, config) -> None:
        # Passing no http forces audit_async to spin up its own AsyncClient
        # and close it on the way out. We can't easily inspect aclose, so this
        # is a smoke test that the no-http path runs end-to-end.
        from chill_out.runner import audit_async
        from chill_out.state import ChillOutState

        eco = _FakeEcosystem(packages=[], data={})
        report = await audit_async(ChillOutState.empty(), eco, config=config, now=now)
        assert report.entries == []

    async def test_loads_config_when_not_supplied(self, now, tmp_path: Path) -> None:
        # When config is None, audit_async calls load_config(eco.root, eco.kind).
        # _FakeEcosystem.root is "/tmp"; load_config there falls through to
        # built-in defaults, which is fine for an empty state.
        from chill_out.runner import audit_async
        from chill_out.state import ChillOutState

        eco = _FakeEcosystem(packages=[], data={})
        eco.root = tmp_path
        report = await audit_async(ChillOutState.empty(), eco, now=now)
        assert report.entries == []


class TestRunnerCoverageGapFillers:
    """Sweep up the last few defensive branches in `runner.py`."""

    async def test_check_async_skips_when_fetch_raises_registry_error(self, now, config) -> None:
        """`fetch_package` raising `RegistryError` lands the package in `skipped`."""
        from chill_out.exceptions import RegistryError

        class _BoomEcosystem(_FakeEcosystem):
            async def fetch_package(self, name, http):
                raise RegistryError(f"registry blew up for {name}")

        eco = _BoomEcosystem(
            packages=[InstalledPackage(name="storm", version="1.0.0", ecosystem=EcosystemKind.NPM)],
            data={},
        )
        report = await check_async(eco, config=config, now=now)
        assert report.violations == []
        assert len(report.skipped) == 1
        assert report.skipped[0].package.name == "storm"
        assert "registry blew up" in report.skipped[0].reason

    def test_check_sync_wrapper_runs_async_and_returns_report(self, now, config, tmp_path) -> None:
        """The sync `check()` wrapper threads through to `check_async`."""
        from chill_out.runner import check

        eco = _FakeEcosystem(
            packages=[InstalledPackage(name="ok", version="1.0.0", ecosystem=EcosystemKind.NPM)],
            data={
                "ok": PackageInfo(
                    name="ok",
                    releases={"1.0.0": PackageRelease(version="1.0.0", published=now.subtract(days=200))},
                )
            },
        )
        # Inject our fake by patching `detect_ecosystem` for this call.
        import chill_out.runner as runner_mod

        original = runner_mod.detect_ecosystem
        setattr(runner_mod, "detect_ecosystem", lambda root: eco)
        try:
            report = check(tmp_path, config=config)
        finally:
            setattr(runner_mod, "detect_ecosystem", original)
        assert report.violations == []
        assert report.checked[0].name == "ok"

    async def test_plan_fixes_skips_ancestor_not_installed(self, now, config) -> None:
        """Ancestors in `via_chain` that aren't in the installed set are ignored."""
        from chill_out.models import CheckReport, SafeVersion, VersionManifest
        from chill_out.runner import plan_fixes_async

        # via_chain = ("ghost", "real-parent"). 'ghost' is not in installed_by_name
        # so the loop hits the `continue` at line 317 before falling through to
        # 'real-parent', which has a permissive manifest.
        installed = InstalledPackage(
            name="child",
            version="2.0.0",
            ecosystem=EcosystemKind.NPM,
            via_chain=("ghost", "real-parent"),
        )
        principal = InstalledPackage(name="real-parent", version="1.0.0", ecosystem=EcosystemKind.NPM)
        v = Violation(
            package=installed,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=now.subtract(days=2),
            safe_version=SafeVersion("1.9.0", 100),
        )
        report = CheckReport(
            ecosystem=EcosystemKind.NPM,
            checked=[installed, principal],
            violations=[v],
        )
        manifests = {
            ("real-parent", "1.0.0"): VersionManifest("real-parent", "1.0.0", deps={"child": ">=1.0"}),
        }
        eco = _FakeEcosystem(packages=[installed, principal], data={}, manifests=manifests)
        plan = await plan_fixes_async(report, eco, config=config, now=now)
        assert len(plan.actions) == 1
        assert plan.actions[0].package == "child"
        assert plan.actions[0].version == "1.9.0"

    async def test_plan_fixes_skips_ancestor_not_declaring_dep(self, now, config) -> None:
        """If an ancestor's manifest doesn't list the violating dep, skip it."""
        from chill_out.models import CheckReport, SafeVersion, VersionManifest
        from chill_out.runner import plan_fixes_async

        principal = InstalledPackage(name="parent", version="1.0.0", ecosystem=EcosystemKind.NPM)
        installed = InstalledPackage(
            name="child",
            version="2.0.0",
            ecosystem=EcosystemKind.NPM,
            via_chain=("parent",),
        )
        v = Violation(
            package=installed,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=now.subtract(days=2),
            safe_version=SafeVersion("1.9.0", 100),
        )
        report = CheckReport(
            ecosystem=EcosystemKind.NPM,
            checked=[installed, principal],
            violations=[v],
        )
        # Parent's manifest exists but does NOT declare 'child' as a dep.
        manifests = {
            ("parent", "1.0.0"): VersionManifest("parent", "1.0.0", deps={"unrelated": ">=1.0"}),
        }
        eco = _FakeEcosystem(packages=[installed, principal], data={}, manifests=manifests)
        plan = await plan_fixes_async(report, eco, config=config, now=now)
        # No conflict found, so we get a direct pin on child.
        assert len(plan.actions) == 1
        assert plan.actions[0].package == "child"
        assert plan.actions[0].version == "1.9.0"

    async def test_plan_fixes_unfixable_when_principal_info_missing(self, now, config) -> None:
        """Conflict detected but `fetch_package(via)` returns None -> unfixable."""
        from chill_out.models import CheckReport, SafeVersion, VersionManifest
        from chill_out.runner import plan_fixes_async

        principal = InstalledPackage(name="parent", version="2.0.0", ecosystem=EcosystemKind.NPM)
        installed = InstalledPackage(
            name="child",
            version="2.0.0",
            ecosystem=EcosystemKind.NPM,
            via_chain=("parent",),
        )
        v = Violation(
            package=installed,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=now.subtract(days=2),
            safe_version=SafeVersion("1.9.0", 100),
        )
        report = CheckReport(
            ecosystem=EcosystemKind.NPM,
            checked=[installed, principal],
            violations=[v],
        )
        manifests = {
            ("parent", "2.0.0"): VersionManifest("parent", "2.0.0", deps={"child": ">=2.0"}),
        }

        class _ConflictEcosystem(_FakeEcosystem):
            def range_satisfies(self, version, range_spec):
                return False  # forces conflict path

        # `data={"parent": None}` -> fetch_package returns None.
        eco = _ConflictEcosystem(
            packages=[installed, principal],
            data={"parent": None},
            manifests=manifests,
        )
        plan = await plan_fixes_async(report, eco, config=config, now=now)
        assert plan.actions == []
        assert len(plan.unfixable) == 1
        assert "release index could not be" in plan.unfixable[0].reason

    def test_candidate_principal_versions_unparsable_installed_returns_empty(self, now, config) -> None:
        """When the installed principal version can't be parsed, return `[]`."""
        from chill_out.runner import candidate_principal_versions

        info = PackageInfo(
            name="parent",
            releases={"1.0.0": PackageRelease(version="1.0.0", published=now.subtract(days=200))},
        )

        def _broken_parser(_: str):
            return None

        out = candidate_principal_versions(info, "not-a-version", config, _broken_parser, now)
        assert out == []

    def test_candidate_principal_versions_filters_out_cooldown_violators(self, now, config) -> None:
        """Older versions still inside the cooldown window are filtered out."""
        from chill_out.ecosystems.npm.backend import NpmEcosystem
        from chill_out.runner import candidate_principal_versions

        parser = NpmEcosystem(root=Path("/tmp")).parse_version
        info = PackageInfo(
            name="parent",
            releases={
                # Older than installed but still inside the cooldown -> filtered.
                "1.5.0": PackageRelease(version="1.5.0", published=now.subtract(days=1)),
                # Older than installed and past cooldown -> kept.
                "1.0.0": PackageRelease(version="1.0.0", published=now.subtract(days=200)),
            },
        )
        out = candidate_principal_versions(info, "2.0.0", config, parser, now)
        assert out == ["1.0.0"]

    def test_dedupe_actions_falls_back_to_lexical_for_invalid_versions(self) -> None:
        """When `Version()` can't parse a candidate, fall back to lexical compare."""
        from chill_out.runner import dedupe_actions

        # Both versions fail PEP 440 parsing -> lexical fallback decides.
        actions = [
            FixAction(package="weird", version="zebra"),
            FixAction(package="weird", version="apple"),
        ]
        out = dedupe_actions(actions)
        assert len(out) == 1
        assert out[0].version == "apple"

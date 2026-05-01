"""Unit tests for reporting helpers."""

import functools
import io

import pendulum
from chill_out.config import ChillOutConfig
from chill_out.constants import EcosystemKind, ReleaseType
from chill_out.ecosystems.npm.backend import NpmEcosystem
from chill_out.models import CheckReport, InstalledPackage, SafeVersion, SkipReason, Violation
from chill_out.render import format_package_node, render_include_groups, render_thresholds
from chill_out.render import render_report as _render_report
from rich.console import Console

_DEFAULT_CFG = ChillOutConfig(
    cooldown_days={ReleaseType.MAJOR: 30, ReleaseType.MINOR: 10, ReleaseType.PATCH: 7, ReleaseType.DEFAULT: 5}
)
# All reporting tests use semver-shaped versions, so wire in the npm parser.
_DEFAULT_PARSER = NpmEcosystem(root=__import__("pathlib").Path("/tmp")).parse_version
render_report = functools.partial(_render_report, config=_DEFAULT_CFG, parser=_DEFAULT_PARSER)


def _capture(fn, *args, **kwargs) -> str:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=200)
    console.print(fn(*args, **kwargs))
    return buf.getvalue()


class TestRenderThresholds:
    def test_lists_all_release_types(self) -> None:
        cfg = ChillOutConfig(
            cooldown_days={ReleaseType.MAJOR: 30, ReleaseType.MINOR: 10, ReleaseType.PATCH: 7, ReleaseType.DEFAULT: 5}
        )
        out = _capture(render_thresholds, cfg)
        for rel_type in ("major", "minor", "patch", "default"):
            assert rel_type in out


class TestRenderReport:
    def test_success_message(self) -> None:
        report = CheckReport(
            ecosystem=EcosystemKind.NPM,
            checked=[InstalledPackage(name="x", version="1.0.0", ecosystem=EcosystemKind.NPM)],
        )
        out = _capture(render_report, report)
        assert "No cooldown violations" in out

    def test_violation_table(self) -> None:
        pkg = InstalledPackage(name="x", version="2.0.0", ecosystem=EcosystemKind.NPM)
        v = Violation(
            package=pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.now("UTC"),
            safe_version=SafeVersion(version="1.5.0", age_days=200),
        )
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[pkg], violations=[v])
        out = _capture(render_report, report)
        assert "x" in out
        assert "1.5.0" in out
        assert "violation" in out

    def test_violation_table_fast_omits_strategy_column(self) -> None:
        pkg = InstalledPackage(name="x", version="2.0.0", ecosystem=EcosystemKind.NPM)
        v = Violation(
            package=pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.now("UTC"),
            safe_version=None,
        )
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[pkg], violations=[v])
        out = _capture(render_report, report, fast=True)
        assert "Strategy" not in out

    def test_strategy_column_for_principal_violation(self) -> None:
        pkg = InstalledPackage(name="x", version="2.0.0", ecosystem=EcosystemKind.NPM)
        v = Violation(
            package=pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.now("UTC"),
            safe_version=SafeVersion(version="1.5.0", age_days=200),
        )
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[pkg], violations=[v])
        out = _capture(render_report, report)
        assert "Strategy" in out
        # Principal pin renders inline (no tree branches needed).
        assert "x" in out and "1.5.0" in out
        assert "->" in out
        assert "200d old" in out

    def test_strategy_column_for_transitive_violation_renders_chain(self) -> None:
        principal = InstalledPackage(name="principal", version="1.0.0", ecosystem=EcosystemKind.NPM)
        pkg = InstalledPackage(name="leaf", version="2.0.0", ecosystem=EcosystemKind.NPM, via_chain=("principal",))
        v = Violation(
            package=pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.now("UTC"),
            safe_version=SafeVersion(version="1.9.0", age_days=42),
        )
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[pkg, principal], violations=[v])
        out = _capture(render_report, report)
        # The strategy column shows the via_chain leading to the explicit leaf
        # pin so the reader knows it's the transitive that needs pinning, not
        # the principal.
        assert "Strategy" in out
        assert "principal" in out
        assert "1.9.0" in out
        assert "42d old" in out
        # The leaf pin uses the arrow marker.
        assert "leaf" in out and "->" in out

    def test_strategy_column_when_no_safe_version(self) -> None:
        pkg = InstalledPackage(name="x", version="2.0.0", ecosystem=EcosystemKind.NPM)
        v = Violation(
            package=pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.now("UTC"),
            safe_version=None,
        )
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[pkg], violations=[v])
        out = _capture(render_report, report)
        assert "no safe version found" in out

    def test_transitive_violation_renders_dep_tree(self) -> None:
        principal = InstalledPackage(name="principal", version="1.0.0", ecosystem=EcosystemKind.NPM)
        pkg = InstalledPackage(name="t", version="2.0.0", ecosystem=EcosystemKind.NPM, via_chain=("principal",))
        v = Violation(
            package=pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.now("UTC"),
        )
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[pkg, principal], violations=[v])
        out = _capture(render_report, report)
        # Principal sits above the violating leaf, with the leaf indented.
        assert "principal" in out
        assert "1.0.0" in out  # principal version pulled from installed index
        assert "t" in out
        assert "2.0.0" in out
        # The leaf annotation should call out the age vs limit explicitly.
        assert "age 2d" in out
        assert "30d" in out
        # Tree connector glyph appears when there is a chain.
        assert "└──" in out or "└── " in out

    def test_multi_level_via_chain_renders_intermediate(self) -> None:
        principal = InstalledPackage(name="principal", version="1.0.0", ecosystem=EcosystemKind.NPM)
        intermediate = InstalledPackage(name="intermediate", version="0.5.0", ecosystem=EcosystemKind.NPM)
        pkg = InstalledPackage(
            name="leaf",
            version="2.0.0",
            ecosystem=EcosystemKind.NPM,
            via_chain=("intermediate", "principal"),
        )
        v = Violation(
            package=pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.now("UTC"),
        )
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[pkg, principal, intermediate], violations=[v])
        out = _capture(render_report, report)
        # All three names should appear in the tree.
        assert "principal" in out
        assert "intermediate" in out
        assert "leaf" in out
        # Intermediate version comes from the installed index too.
        assert "0.5.0" in out

    def test_shared_violation_annotates_strategy_with_member_owners(self) -> None:
        # Tier 1: cross-member shared violations should be flagged in the strategy.
        pkg = InstalledPackage(
            name="lodash",
            version="4.0.0",
            ecosystem=EcosystemKind.NPM,
            via_chain=("react",),
            member_owners=("api", "backend"),
        )
        principal = InstalledPackage(name="react", version="18.0.0", ecosystem=EcosystemKind.NPM)
        v = Violation(
            package=pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.now("UTC"),
            safe_version=SafeVersion(version="3.9.0", age_days=300),
        )
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[pkg, principal], violations=[v])
        out = _capture(render_report, report)
        assert "shared" in out
        assert "api" in out and "backend" in out
        assert "overrides" in out

    def test_unshared_violation_omits_shared_annotation(self) -> None:
        pkg = InstalledPackage(
            name="lodash",
            version="4.0.0",
            ecosystem=EcosystemKind.NPM,
            member_owners=("api",),
        )
        v = Violation(
            package=pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.now("UTC"),
            safe_version=SafeVersion(version="3.9.0", age_days=300),
        )
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[pkg], violations=[v])
        out = _capture(render_report, report)
        assert "shared" not in out
        assert "overrides" not in out


class TestRenderIncludeGroups:
    def test_empty_groups_renders_explicit_warning(self) -> None:
        """An empty `include_groups` config prints the explicit '(none -- nothing will be checked)' line."""
        cfg = ChillOutConfig(include_groups=())
        out = _capture(render_include_groups, cfg)
        assert "nothing will be checked" in out


class TestFormatPackageNode:
    def test_version_none_renders_only_name(self) -> None:
        """`format_package_node` returns just the bold name when no version is known."""
        assert format_package_node("foo", None) == "[bold]foo[/bold]"


class TestRenderReportSkipsAndIntermediates:
    def test_success_line_includes_skipped_count(self) -> None:
        """A clean run with skipped packages prints the trailing skipped-count parenthetical."""
        pkg = InstalledPackage(name="x", version="1.0.0", ecosystem=EcosystemKind.NPM)
        skipped_pkg = InstalledPackage(name="ghost", version="0.0.0", ecosystem=EcosystemKind.NPM)
        report = CheckReport(
            ecosystem=EcosystemKind.NPM,
            checked=[pkg],
            skipped=[SkipReason(package=skipped_pkg, reason="not in registry")],
        )
        out = _capture(render_report, report)
        assert "1 package(s) skipped" in out

    def test_violation_table_includes_skipped_count(self) -> None:
        """A run with both violations and skipped packages prints both the table and the skipped-count line."""
        pkg = InstalledPackage(name="lodash", version="4.0.0", ecosystem=EcosystemKind.NPM)
        ghost = InstalledPackage(name="ghost", version="0.0.0", ecosystem=EcosystemKind.NPM)
        v = Violation(
            package=pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.now("UTC"),
            safe_version=SafeVersion(version="3.9.0", age_days=300),
        )
        report = CheckReport(
            ecosystem=EcosystemKind.NPM,
            checked=[pkg],
            violations=[v],
            skipped=[SkipReason(package=ghost, reason="not in registry")],
        )
        out = _capture(render_report, report)
        assert "1 package(s) skipped" in out

    def test_limit_tree_renders_dim_placeholder_for_unknown_intermediate(self) -> None:
        """When an intermediate ancestor isn't in `installed_index`, the limit tree shows a dim '-'."""
        pkg = InstalledPackage(
            name="leaf",
            version="2.0.0",
            ecosystem=EcosystemKind.NPM,
            via_chain=("ghost-intermediate", "principal"),
        )
        principal = InstalledPackage(name="principal", version="1.0.0", ecosystem=EcosystemKind.NPM)
        v = Violation(
            package=pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.now("UTC"),
            safe_version=SafeVersion(version="1.9.0", age_days=300),
        )
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[pkg, principal], violations=[v])
        out = _capture(render_report, report)
        assert "ghost-intermediate" in out

    def test_strategy_renders_intermediate_for_three_deep_chain(self) -> None:
        """A `via_chain` of length 3+ renders each intermediate as a dim node in the strategy tree."""
        pkg = InstalledPackage(
            name="leaf",
            version="2.0.0",
            ecosystem=EcosystemKind.NPM,
            via_chain=("middle", "intermediate", "principal"),
        )
        principal = InstalledPackage(name="principal", version="1.0.0", ecosystem=EcosystemKind.NPM)
        intermediate = InstalledPackage(name="intermediate", version="0.5.0", ecosystem=EcosystemKind.NPM)
        middle = InstalledPackage(name="middle", version="0.3.0", ecosystem=EcosystemKind.NPM)
        v = Violation(
            package=pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.now("UTC"),
            safe_version=SafeVersion(version="1.9.0", age_days=300),
        )
        report = CheckReport(
            ecosystem=EcosystemKind.NPM,
            checked=[pkg, principal, intermediate, middle],
            violations=[v],
        )
        out = _capture(render_report, report)
        assert "intermediate" in out
        assert "middle" in out


class TestRenderAuditReport:
    """`render_audit_report` produces a Group with one table per non-empty bucket."""

    def _entry(
        self,
        package: str,
        status,  # AuditStatus, imported lazily inside helpers below
        avoided_version: str = "2.0.0",
        current_age_days: int | None = 100,
        cooldown_days: int = 30,
        detail: str | None = None,
    ):
        from pathlib import Path

        from chill_out.models import AuditedPin
        from chill_out.state import AvoidingRelease, ManagedPin, PinMechanism

        pin = ManagedPin(
            package=package,
            ecosystem=EcosystemKind.NPM,
            mechanism=PinMechanism.DIRECT,
            manifest_path=Path("package.json"),
            pinned_spec=f"{package}==1.0.0",
            applied_at=pendulum.datetime(2026, 1, 1, tz="UTC"),
            avoiding=AvoidingRelease(
                version=avoided_version,
                release_type=ReleaseType.MAJOR,
                published_at=pendulum.datetime(2026, 1, 1, tz="UTC"),
                cooldown_days=cooldown_days,
            ),
        )
        return AuditedPin(
            pin=pin,
            status=status,
            current_age_days=current_age_days,
            cooldown_days=cooldown_days,
            detail=detail,
        )

    def test_empty_report_renders_success_line(self) -> None:
        from chill_out.models import AuditReport
        from chill_out.render import render_audit_report

        report = AuditReport(ecosystem=EcosystemKind.NPM, entries=[])
        out = _capture(render_audit_report, report)
        assert "No managed pins to audit" in out
        assert "npm" in out

    def test_renders_all_four_buckets(self) -> None:
        from chill_out.constants import AuditStatus
        from chill_out.models import AuditReport
        from chill_out.render import render_audit_report

        report = AuditReport(
            ecosystem=EcosystemKind.NPM,
            entries=[
                self._entry("stale-pkg", AuditStatus.STALE, current_age_days=200),
                self._entry("yanked-pkg", AuditStatus.YANKED, current_age_days=300),
                self._entry("fresh-pkg", AuditStatus.FRESH, current_age_days=5),
                self._entry("unknown-pkg", AuditStatus.UNKNOWN, current_age_days=None, detail="not found"),
            ],
        )
        out = _capture(render_audit_report, report)
        # Headline carries every bucket count.
        assert "1 stale" in out
        assert "1 yanked" in out
        assert "1 fresh" in out
        assert "1 unknown" in out
        # Each table title appears.
        assert "Stale (pin can be retired)" in out
        assert "Yanked (pin can be retired)" in out
        assert "Fresh (still in cooldown)" in out
        assert "Unknown (review manually)" in out
        # Package names land in their tables.
        assert "stale-pkg" in out
        assert "yanked-pkg" in out
        assert "fresh-pkg" in out
        assert "unknown-pkg" in out
        # Unknown table shows the detail string.
        assert "not found" in out
        # Age formatting: known ages render as `<n>d / <limit>d`, unknown as `?`.
        assert "200d / 30d" in out
        assert "?" in out

    def test_actionable_only_buckets_renders_without_fresh_or_unknown_sections(self) -> None:
        from chill_out.constants import AuditStatus
        from chill_out.models import AuditReport
        from chill_out.render import render_audit_report

        report = AuditReport(
            ecosystem=EcosystemKind.NPM,
            entries=[self._entry("stale-pkg", AuditStatus.STALE)],
        )
        out = _capture(render_audit_report, report)
        assert "Stale (pin can be retired)" in out
        assert "Fresh (still in cooldown)" not in out
        assert "Unknown (review manually)" not in out

    def test_unknown_table_falls_back_when_detail_absent(self) -> None:
        from chill_out.constants import AuditStatus
        from chill_out.models import AuditReport
        from chill_out.render import render_audit_report

        # An UNKNOWN entry without a detail string still renders cleanly.
        report = AuditReport(
            ecosystem=EcosystemKind.NPM,
            entries=[self._entry("mystery", AuditStatus.UNKNOWN, current_age_days=None, detail=None)],
        )
        out = _capture(render_audit_report, report)
        assert "mystery" in out
        # Detail column shows the placeholder.
        assert "-" in out

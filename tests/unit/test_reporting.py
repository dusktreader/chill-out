"""Unit tests for reporting helpers."""

from __future__ import annotations

import functools
import io

import pendulum
from chill_out.config import CooldownConfig
from chill_out.constants import ReleaseType, EcosystemKind
from chill_out.models import CheckReport, InstalledPackage, SafeVersion, Violation
from chill_out.reporting import render_report as _render_report
from chill_out.reporting import render_thresholds
from rich.console import Console

_DEFAULT_CFG = CooldownConfig(
    cooldown_days={ReleaseType.MAJOR: 30, ReleaseType.MINOR: 10, ReleaseType.PATCH: 7, ReleaseType.DEFAULT: 5}
)
render_report = functools.partial(_render_report, config=_DEFAULT_CFG)


def _capture(fn, *args, **kwargs) -> str:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=200)
    fn(*args, console, **kwargs)
    return buf.getvalue()


class TestRenderThresholds:
    def test_lists_all_release_types(self) -> None:
        cfg = CooldownConfig(cooldown_days={ReleaseType.MAJOR: 30, ReleaseType.MINOR: 10, ReleaseType.PATCH: 7, ReleaseType.DEFAULT: 5})
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
        pkg = InstalledPackage(
            name="leaf", version="2.0.0", ecosystem=EcosystemKind.NPM, via_chain=("principal",)
        )
        v = Violation(
            package=pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.now("UTC"),
            safe_version=SafeVersion(version="1.9.0", age_days=42),
        )
        report = CheckReport(
            ecosystem=EcosystemKind.NPM, checked=[pkg, principal], violations=[v]
        )
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
        pkg = InstalledPackage(
            name="t", version="2.0.0", ecosystem=EcosystemKind.NPM, via_chain=("principal",)
        )
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
        report = CheckReport(
            ecosystem=EcosystemKind.NPM, checked=[pkg, principal, intermediate], violations=[v]
        )
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
        report = CheckReport(
            ecosystem=EcosystemKind.NPM, checked=[pkg, principal], violations=[v]
        )
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

"""Unit tests for reporting helpers."""

from __future__ import annotations

import io

import pendulum
from chill_out.config import CooldownConfig
from chill_out.constants import ReleaseType, EcosystemKind
from chill_out.models import CheckReport, InstalledPackage, SafeVersion, Violation
from chill_out.reporting import render_report, render_thresholds
from rich.console import Console


def _capture(fn, *args, **kwargs) -> str:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=200)
    fn(*args, console, **kwargs)
    return buf.getvalue()


class TestRenderThresholds:
    def test_lists_all_release_types(self) -> None:
        cfg = CooldownConfig(days={ReleaseType.MAJOR: 30, ReleaseType.MINOR: 10, ReleaseType.PATCH: 7, ReleaseType.DEFAULT: 5})
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

    def test_violation_table_fast_omits_safe_column(self) -> None:
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
        assert "Suggested" not in out

    def test_via_column_appears_when_transitive(self) -> None:
        pkg = InstalledPackage(name="t", version="2.0.0", ecosystem=EcosystemKind.NPM, via_chain=("principal",))
        v = Violation(
            package=pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.now("UTC"),
        )
        report = CheckReport(ecosystem=EcosystemKind.NPM, checked=[pkg], violations=[v])
        out = _capture(render_report, report)
        assert "principal" in out

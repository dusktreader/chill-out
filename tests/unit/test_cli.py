"""Unit tests for the CLI surface (no real registry calls)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pendulum
from chill_out import __version__
from chill_out.cli.main import cli
from chill_out.constants import EcosystemKind
from chill_out.models import CheckReport, InstalledPackage, SafeVersion, Violation
from typer.testing import CliRunner


def _patch_check_returning(report: CheckReport):
    async def _fake(eco, **kw):
        return report

    return patch("chill_out.cli.main.check_async", side_effect=_fake)


class TestVersion:
    def test_prints_version(self) -> None:
        result = CliRunner().invoke(cli, ["version"])
        assert result.exit_code == 0
        assert __version__ in result.stdout


class TestShowConfig:
    def test_runs_against_a_pypi_project(self, pypi_project: Path) -> None:
        result = CliRunner().invoke(cli, ["show-config", "--root", str(pypi_project)])
        assert result.exit_code == 0
        assert "pypi" in result.stdout
        # Threshold table renders release types
        assert "patch" in result.stdout
        assert "major" in result.stdout


class TestCheck:
    def test_detects_no_violations(self, pypi_project: Path) -> None:
        report = CheckReport(
            ecosystem=EcosystemKind.PYPI,
            checked=[InstalledPackage(name="x", version="1.0.0", ecosystem=EcosystemKind.PYPI)],
        )
        with _patch_check_returning(report):
            result = CliRunner().invoke(cli, ["check", "--root", str(pypi_project)])
        assert result.exit_code == 0
        assert "No cooldown violations" in result.stdout

    def test_exits_nonzero_on_violations(self, pypi_project: Path) -> None:
        pkg = InstalledPackage(name="x", version="2.0.0", ecosystem=EcosystemKind.PYPI)
        from chill_out.constants import ReleaseType

        v = Violation(
            package=pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.now("UTC"),
            safe_version=SafeVersion(version="1.5.0", age_days=200),
        )
        report = CheckReport(ecosystem=EcosystemKind.PYPI, checked=[pkg], violations=[v])
        with _patch_check_returning(report):
            result = CliRunner().invoke(cli, ["check", "--root", str(pypi_project)])
        assert result.exit_code == 2  # ExitCode.COOLDOWN_VIOLATION

    def test_fix_with_fast_is_rejected(self, pypi_project: Path) -> None:
        result = CliRunner().invoke(cli, ["check", "--root", str(pypi_project), "--fix", "--fast"])
        assert result.exit_code != 0
        assert "fast" in result.stdout.lower() or "fast" in (result.stderr or "").lower()

    def test_quiet_omits_threshold_table(self, pypi_project: Path) -> None:
        report = CheckReport(ecosystem=EcosystemKind.PYPI, checked=[])
        with _patch_check_returning(report):
            result = CliRunner().invoke(cli, ["check", "--root", str(pypi_project), "--quiet"])
        assert result.exit_code == 0
        assert "Cooldown thresholds" not in result.stdout

    def test_fix_invokes_apply_fixes(self, pypi_project: Path) -> None:
        pkg = InstalledPackage(name="x", version="2.0.0", ecosystem=EcosystemKind.PYPI)
        from chill_out.constants import ReleaseType

        v = Violation(
            package=pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.now("UTC"),
            safe_version=SafeVersion(version="1.5.0", age_days=200),
        )
        report = CheckReport(ecosystem=EcosystemKind.PYPI, checked=[pkg], violations=[v])
        with (
            _patch_check_returning(report),
            patch(
                "chill_out.ecosystems.pypi.PypiEcosystem.apply_fixes",
                return_value=["pinned x -> 1.5.0", "ran: uv lock"],
            ) as apply_mock,
        ):
            result = CliRunner().invoke(cli, ["check", "--root", str(pypi_project), "--fix"])
        assert apply_mock.called
        # The recheck still finds the (mocked) violation, so we still exit 2.
        assert result.exit_code == 2
        # The default --fix flow also re-runs the check to confirm the fix.
        assert "Re-checking" in result.stdout

    def test_fix_with_no_recheck_skips_recheck(self, pypi_project: Path) -> None:
        pkg = InstalledPackage(name="x", version="2.0.0", ecosystem=EcosystemKind.PYPI)
        from chill_out.constants import ReleaseType

        v = Violation(
            package=pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.now("UTC"),
            safe_version=SafeVersion(version="1.5.0", age_days=200),
        )
        report = CheckReport(ecosystem=EcosystemKind.PYPI, checked=[pkg], violations=[v])
        with (
            _patch_check_returning(report) as check_mock,
            patch(
                "chill_out.ecosystems.pypi.PypiEcosystem.apply_fixes",
                return_value=["pinned x -> 1.5.0"],
            ),
        ):
            result = CliRunner().invoke(
                cli, ["check", "--root", str(pypi_project), "--fix", "--no-recheck"]
            )
        assert result.exit_code == 2
        # Only the initial check ran; --no-recheck suppresses the follow-up.
        assert check_mock.call_count == 1
        assert "Re-checking" not in result.stdout
        assert "Re-run `chill-out check`" in result.stdout

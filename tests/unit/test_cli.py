"""Unit tests for the CLI surface (no real registry calls)."""

from pathlib import Path
from unittest.mock import patch

import pendulum
from chill_out import __version__
from chill_out.cli.main import cli
from chill_out.constants import EcosystemKind
from chill_out.models import AppliedFix, AppliedFixes, CheckReport, FixAction, InstalledPackage, SafeVersion, Violation
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

    def test_quiet_omits_threshold_table(self, pypi_project: Path) -> None:
        report = CheckReport(ecosystem=EcosystemKind.PYPI, checked=[])
        with _patch_check_returning(report):
            result = CliRunner().invoke(cli, ["check", "--root", str(pypi_project), "--quiet"])
        assert result.exit_code == 0
        assert "Cooldown thresholds" not in result.stdout

    def test_check_does_not_accept_fix_flag(self, pypi_project: Path) -> None:
        # `--fix` lives on `chill-out fix` now, not on `check`.
        result = CliRunner().invoke(cli, ["check", "--root", str(pypi_project), "--fix"])
        assert result.exit_code != 0


class TestFix:
    def test_invokes_apply_fixes(self, pypi_project: Path) -> None:
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
        fix_action = FixAction(package="x", version="1.5.0")
        with (
            _patch_check_returning(report),
            patch(
                "chill_out.ecosystems.pypi.backend.PypiEcosystem.apply_fixes",
                return_value=AppliedFixes(
                    entries=[
                        AppliedFix(
                            action=fix_action,
                            pinned_spec="x==1.5.0",
                            via_overrides=False,
                            manifest_path=Path("pyproject.toml"),
                        )
                    ],
                    log=["pinned x -> 1.5.0", "ran: uv lock"],
                ),
            ) as apply_mock,
            patch(
                "chill_out.ecosystems.pypi.backend.PypiEcosystem.apply_override_fixes",
                return_value=AppliedFixes(
                    entries=[
                        AppliedFix(
                            action=fix_action,
                            pinned_spec="x==1.5.0",
                            via_overrides=True,
                            manifest_path=Path("pyproject.toml"),
                        )
                    ],
                    log=["overrode x==1.5.0 (workspace root)", "ran: uv lock"],
                ),
            ),
        ):
            result = CliRunner().invoke(cli, ["fix", "--root", str(pypi_project)])
        assert apply_mock.called
        # The recheck still finds the (mocked) violation, so we still exit 2.
        assert result.exit_code == 2
        # The default fix flow also re-runs the check to confirm the fix.
        assert "Re-checking" in result.stdout

    def test_no_recheck_skips_recheck(self, pypi_project: Path) -> None:
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
        fix_action = FixAction(package="x", version="1.5.0")
        with (
            _patch_check_returning(report) as check_mock,
            patch(
                "chill_out.ecosystems.pypi.backend.PypiEcosystem.apply_fixes",
                return_value=AppliedFixes(
                    entries=[
                        AppliedFix(
                            action=fix_action,
                            pinned_spec="x==1.5.0",
                            via_overrides=False,
                            manifest_path=Path("pyproject.toml"),
                        )
                    ],
                    log=["pinned x -> 1.5.0"],
                ),
            ),
        ):
            result = CliRunner().invoke(cli, ["fix", "--root", str(pypi_project), "--no-recheck"])
        assert result.exit_code == 2
        # Only the initial check ran; --no-recheck suppresses the follow-up.
        assert check_mock.call_count == 1
        assert "Re-checking" not in result.stdout
        assert "Re-run `chill-out check`" in result.stdout


class TestFixStateLifecycle:
    """Tests for `.chill-out-state.json` integration in `chill-out fix`."""

    def _violation_pkg(self) -> InstalledPackage:
        return InstalledPackage(name="x", version="2.0.0", ecosystem=EcosystemKind.PYPI)

    def _violation_for(self, pkg: InstalledPackage) -> Violation:
        from chill_out.constants import ReleaseType

        return Violation(
            package=pkg,
            release_type=ReleaseType.MAJOR,
            age_days=2,
            limit_days=30,
            published=pendulum.now("UTC"),
            safe_version=SafeVersion(version="1.5.0", age_days=200),
        )

    def test_writes_state_after_successful_fix(self, pypi_project: Path) -> None:
        from chill_out.state import STATE_FILENAME, ChillOutState

        pkg = self._violation_pkg()
        v = self._violation_for(pkg)
        report = CheckReport(ecosystem=EcosystemKind.PYPI, checked=[pkg], violations=[v])
        fix_action = FixAction(package="x", version="1.5.0")
        with (
            _patch_check_returning(report),
            patch(
                "chill_out.ecosystems.pypi.backend.PypiEcosystem.apply_fixes",
                return_value=AppliedFixes(
                    entries=[
                        AppliedFix(
                            action=fix_action,
                            pinned_spec="x==1.5.0",
                            via_overrides=False,
                            manifest_path=Path("pyproject.toml"),
                        )
                    ],
                    log=["pinned x -> 1.5.0"],
                ),
            ),
        ):
            CliRunner().invoke(cli, ["fix", "--root", str(pypi_project), "--no-recheck"])

        state_path = pypi_project / STATE_FILENAME
        assert state_path.is_file()
        loaded = ChillOutState.load(pypi_project)
        assert len(loaded.managed_pins) == 1
        assert loaded.managed_pins[0].package == "x"
        assert loaded.managed_pins[0].pinned_spec == "x==1.5.0"

    def test_deletes_state_when_no_pins_remain(self, pypi_project: Path) -> None:
        # Empty report means no fixes applied; nothing to track.
        from chill_out.state import STATE_FILENAME

        report = CheckReport(ecosystem=EcosystemKind.PYPI, checked=[])
        with _patch_check_returning(report):
            CliRunner().invoke(cli, ["fix", "--root", str(pypi_project), "--no-recheck"])
        assert not (pypi_project / STATE_FILENAME).exists()

    def test_cleanup_removes_stale_pins(self, pypi_project: Path) -> None:
        # Pre-seed a state file; the fix run should call remove_managed_pin and regenerate the lock.
        from chill_out.constants import ReleaseType
        from chill_out.state import (
            AvoidingRelease,
            ChillOutState,
            ManagedPin,
            PinMechanism,
            RemovalOutcome,
        )

        stale = ManagedPin(
            package="oldpkg",
            ecosystem=EcosystemKind.PYPI,
            mechanism=PinMechanism.DIRECT,
            manifest_path=Path("pyproject.toml"),
            pinned_spec="oldpkg==1.0.0",
            applied_at=pendulum.datetime(2026, 1, 1, tz="UTC"),
            avoiding=AvoidingRelease(
                version="2.0.0",
                release_type=ReleaseType.MAJOR,
                published_at=pendulum.datetime(2026, 1, 1, tz="UTC"),
                cooldown_days=30,
            ),
        )
        state = ChillOutState.empty()
        state.managed_pins.append(stale)
        state.save(pypi_project)

        report = CheckReport(ecosystem=EcosystemKind.PYPI, checked=[])
        with (
            _patch_check_returning(report),
            patch(
                "chill_out.ecosystems.pypi.backend.PypiEcosystem.remove_managed_pin",
                return_value=RemovalOutcome.REMOVED,
            ) as remove_mock,
            patch(
                "chill_out.ecosystems.pypi.backend.PypiEcosystem.regenerate_lockfile",
                return_value="ran: uv lock",
            ) as regen_mock,
        ):
            result = CliRunner().invoke(cli, ["fix", "--root", str(pypi_project), "--no-recheck"])
        assert result.exit_code == 0
        assert remove_mock.called
        assert regen_mock.called
        assert "Cleaning up" in result.stdout
        # State should now be gone (everything cleaned, nothing new).
        from chill_out.state import STATE_FILENAME

        assert not (pypi_project / STATE_FILENAME).exists()

    def test_no_cleanup_flag_skips_removal(self, pypi_project: Path) -> None:
        from chill_out.constants import ReleaseType
        from chill_out.state import (
            AvoidingRelease,
            ChillOutState,
            ManagedPin,
            PinMechanism,
        )

        stale = ManagedPin(
            package="oldpkg",
            ecosystem=EcosystemKind.PYPI,
            mechanism=PinMechanism.DIRECT,
            manifest_path=Path("pyproject.toml"),
            pinned_spec="oldpkg==1.0.0",
            applied_at=pendulum.datetime(2026, 1, 1, tz="UTC"),
            avoiding=AvoidingRelease(
                version="2.0.0",
                release_type=ReleaseType.MAJOR,
                published_at=pendulum.datetime(2026, 1, 1, tz="UTC"),
                cooldown_days=30,
            ),
        )
        state = ChillOutState.empty()
        state.managed_pins.append(stale)
        state.save(pypi_project)

        report = CheckReport(ecosystem=EcosystemKind.PYPI, checked=[])
        with (
            _patch_check_returning(report),
            patch(
                "chill_out.ecosystems.pypi.backend.PypiEcosystem.remove_managed_pin",
            ) as remove_mock,
        ):
            CliRunner().invoke(cli, ["fix", "--root", str(pypi_project), "--no-cleanup", "--no-recheck"])
        assert not remove_mock.called


class TestAudit:
    """Tests for the `chill-out audit` subcommand.

    The audit command is plumbing: it loads the state file, hands it to
    `audit_async`, and renders the result. These tests stub `audit_async`
    so they never touch the network and focus on the CLI's exit-code,
    state-file-handling, and rendering contracts.
    """

    def _audit_report(self, *entries):
        from chill_out.models import AuditReport

        return AuditReport(ecosystem=EcosystemKind.PYPI, entries=list(entries))

    def _entry(
        self,
        package: str,
        status,
        avoided: str = "2.0.0",
        age: int | None = 100,
        cooldown: int = 30,
        detail: str | None = None,
    ):
        from chill_out.constants import ReleaseType
        from chill_out.models import AuditedPin
        from chill_out.state import AvoidingRelease, ManagedPin, PinMechanism

        pin = ManagedPin(
            package=package,
            ecosystem=EcosystemKind.PYPI,
            mechanism=PinMechanism.DIRECT,
            manifest_path=Path("pyproject.toml"),
            pinned_spec=f"{package}==1.0.0",
            applied_at=pendulum.datetime(2026, 1, 1, tz="UTC"),
            avoiding=AvoidingRelease(
                version=avoided,
                release_type=ReleaseType.MAJOR,
                published_at=pendulum.datetime(2026, 1, 1, tz="UTC"),
                cooldown_days=cooldown,
            ),
        )
        return AuditedPin(pin=pin, status=status, current_age_days=age, cooldown_days=cooldown, detail=detail)

    def _seed_state(self, root: Path, *pin_packages: str) -> None:
        """Drop a minimal state file with one DIRECT pin per package name."""
        from chill_out.constants import ReleaseType
        from chill_out.state import AvoidingRelease, ChillOutState, ManagedPin, PinMechanism

        state = ChillOutState.empty()
        for pkg in pin_packages:
            state.managed_pins.append(
                ManagedPin(
                    package=pkg,
                    ecosystem=EcosystemKind.PYPI,
                    mechanism=PinMechanism.DIRECT,
                    manifest_path=Path("pyproject.toml"),
                    pinned_spec=f"{pkg}==1.0.0",
                    applied_at=pendulum.datetime(2026, 1, 1, tz="UTC"),
                    avoiding=AvoidingRelease(
                        version="2.0.0",
                        release_type=ReleaseType.MAJOR,
                        published_at=pendulum.datetime(2026, 1, 1, tz="UTC"),
                        cooldown_days=30,
                    ),
                )
            )
        state.save(root)

    def _patch_audit_returning(self, report):
        async def _fake(*args, **kwargs):
            return report

        return patch("chill_out.cli.main.audit_async", side_effect=_fake)

    def test_no_state_file_exits_quietly(self, pypi_project: Path) -> None:
        result = CliRunner().invoke(cli, ["audit", "--root", str(pypi_project)])
        assert result.exit_code == 0
        assert "nothing to audit" in result.stdout

    def test_state_file_with_zero_pins_exits_quietly(self, pypi_project: Path) -> None:
        from chill_out.state import ChillOutState

        # Edge case: a state file present on disk but with empty managed_pins.
        # `chill-out fix` deletes the file in that case, but a hand-edited
        # state could land in this shape and the audit shouldn't blow up.
        ChillOutState.empty().save(pypi_project)
        result = CliRunner().invoke(cli, ["audit", "--root", str(pypi_project)])
        assert result.exit_code == 0
        assert "no managed pins" in result.stdout.lower()

    def test_all_fresh_exits_zero(self, pypi_project: Path) -> None:
        from chill_out.constants import AuditStatus

        self._seed_state(pypi_project, "fresh-pkg")
        report = self._audit_report(self._entry("fresh-pkg", AuditStatus.FRESH, age=5))
        with self._patch_audit_returning(report):
            result = CliRunner().invoke(cli, ["audit", "--root", str(pypi_project)])
        assert result.exit_code == 0
        assert "1 fresh" in result.stdout

    def test_stale_pin_exits_state_stale(self, pypi_project: Path) -> None:
        from chill_out.constants import AuditStatus, ExitCode

        self._seed_state(pypi_project, "stale-pkg")
        report = self._audit_report(self._entry("stale-pkg", AuditStatus.STALE, age=200))
        with self._patch_audit_returning(report):
            result = CliRunner().invoke(cli, ["audit", "--root", str(pypi_project)])
        assert result.exit_code == int(ExitCode.STATE_STALE)
        assert "1 stale" in result.stdout

    def test_yanked_pin_exits_state_stale(self, pypi_project: Path) -> None:
        from chill_out.constants import AuditStatus, ExitCode

        self._seed_state(pypi_project, "yanked-pkg")
        report = self._audit_report(self._entry("yanked-pkg", AuditStatus.YANKED, age=300))
        with self._patch_audit_returning(report):
            result = CliRunner().invoke(cli, ["audit", "--root", str(pypi_project)])
        assert result.exit_code == int(ExitCode.STATE_STALE)
        assert "1 yanked" in result.stdout

    def test_quiet_omits_threshold_table(self, pypi_project: Path) -> None:
        from chill_out.constants import AuditStatus

        self._seed_state(pypi_project, "x")
        report = self._audit_report(self._entry("x", AuditStatus.FRESH))
        with self._patch_audit_returning(report):
            result = CliRunner().invoke(cli, ["audit", "--root", str(pypi_project), "--quiet"])
        assert result.exit_code == 0
        assert "Cooldown thresholds" not in result.stdout


class TestMakeConsole:
    def test_returns_default_width_console_when_stdout_is_tty(self) -> None:
        """`_make_console` honors a TTY by returning a default-width Console."""
        import sys

        from chill_out.cli.main import _make_console

        with patch.object(sys.stdout, "isatty", return_value=True):
            console = _make_console()
        assert console.width != 140 or console.is_terminal


class TestFixCleanupOutcomes:
    """Cover the drift and orphan branches of the cleanup loop in `fix`."""

    def _make_pin(self, package: str = "oldpkg"):
        from chill_out.constants import ReleaseType
        from chill_out.state import AvoidingRelease, ManagedPin, PinMechanism

        return ManagedPin(
            package=package,
            ecosystem=EcosystemKind.PYPI,
            mechanism=PinMechanism.DIRECT,
            manifest_path=Path("pyproject.toml"),
            pinned_spec=f"{package}==1.0.0",
            applied_at=pendulum.datetime(2026, 1, 1, tz="UTC"),
            avoiding=AvoidingRelease(
                version="2.0.0",
                release_type=ReleaseType.MAJOR,
                published_at=pendulum.datetime(2026, 1, 1, tz="UTC"),
                cooldown_days=30,
            ),
        )

    def test_drifted_pin_renders_drift_message(self, pypi_project: Path) -> None:
        """A `RemovalOutcome.DRIFTED` pin prints the yellow 'drift:' diagnostic."""
        from chill_out.state import ChillOutState, RemovalOutcome

        state = ChillOutState.empty()
        state.managed_pins.append(self._make_pin())
        state.save(pypi_project)

        report = CheckReport(ecosystem=EcosystemKind.PYPI, checked=[])
        with (
            _patch_check_returning(report),
            patch(
                "chill_out.ecosystems.pypi.backend.PypiEcosystem.remove_managed_pin",
                return_value=RemovalOutcome.DRIFTED,
            ),
        ):
            result = CliRunner().invoke(cli, ["fix", "--root", str(pypi_project), "--no-recheck"])
        assert result.exit_code == 0
        assert "drift:" in result.stdout

    def test_orphan_pin_renders_orphan_message(self, pypi_project: Path) -> None:
        """A `RemovalOutcome.ORPHAN` pin prints the dim 'orphan:' diagnostic."""
        from chill_out.state import ChillOutState, RemovalOutcome

        state = ChillOutState.empty()
        state.managed_pins.append(self._make_pin())
        state.save(pypi_project)

        report = CheckReport(ecosystem=EcosystemKind.PYPI, checked=[])
        with (
            _patch_check_returning(report),
            patch(
                "chill_out.ecosystems.pypi.backend.PypiEcosystem.remove_managed_pin",
                return_value=RemovalOutcome.ORPHAN,
            ),
        ):
            result = CliRunner().invoke(cli, ["fix", "--root", str(pypi_project), "--no-recheck"])
        assert result.exit_code == 0
        assert "orphan:" in result.stdout


class TestFixUnfixableRendering:
    """Cover the unfixable-violation rendering branch in the `fix` command."""

    def _violating_report(self) -> CheckReport:
        pkg = InstalledPackage(name="bad", version="2.0.0", ecosystem=EcosystemKind.PYPI)
        v = Violation(
            package=pkg,
            release_type=__import__("chill_out").constants.ReleaseType.MAJOR,
            age_days=1,
            limit_days=30,
            published=pendulum.now("UTC"),
            safe_version=SafeVersion(version="1.9.0", age_days=300),
        )
        return CheckReport(ecosystem=EcosystemKind.PYPI, checked=[pkg], violations=[v])

    def test_unfixable_section_rendered_when_plan_has_unfixable_entries(self, pypi_project: Path) -> None:
        """A plan with `unfixable` entries renders the yellow header and per-violation lines."""
        from chill_out.models import FixPlan, UnfixableViolation

        report = self._violating_report()
        violation = report.violations[0]
        plan = FixPlan(actions=[], unfixable=[UnfixableViolation(violation=violation, reason="no safe version")])

        async def fake_plan(*args, **kwargs):
            return plan

        with (
            _patch_check_returning(report),
            patch("chill_out.cli.main.plan_fixes_async", side_effect=fake_plan),
        ):
            result = CliRunner().invoke(cli, ["fix", "--root", str(pypi_project), "--no-recheck"])
        assert result.exit_code != 0  # Violations remain.
        assert "cannot be auto-fixed" in result.stdout
        assert "no safe version" in result.stdout

    def test_no_fixable_violations_rendered_when_plan_actions_empty(self, pypi_project: Path) -> None:
        """A plan with unfixable entries but no actions prints 'No fixable violations.'"""
        from chill_out.models import FixPlan, UnfixableViolation

        report = self._violating_report()
        violation = report.violations[0]
        plan = FixPlan(actions=[], unfixable=[UnfixableViolation(violation=violation, reason="stuck")])

        async def fake_plan(*args, **kwargs):
            return plan

        with (
            _patch_check_returning(report),
            patch("chill_out.cli.main.plan_fixes_async", side_effect=fake_plan),
        ):
            result = CliRunner().invoke(cli, ["fix", "--root", str(pypi_project), "--no-recheck"])
        assert "No fixable violations." in result.stdout


class TestReset:
    """Cover `chill-out reset`: confirmation, rollback (default), --no-rollback, --dry-run, fault-tolerance."""

    def _make_pin(self, package: str = "lodash"):
        from chill_out.constants import EcosystemKind, ReleaseType
        from chill_out.state import AvoidingRelease, ManagedPin, PinMechanism

        return ManagedPin(
            package=package,
            ecosystem=EcosystemKind.PYPI,
            mechanism=PinMechanism.DIRECT,
            manifest_path=Path("pyproject.toml"),
            pinned_spec=f"{package}==1.0.0",
            applied_at=pendulum.datetime(2026, 1, 1, tz="UTC"),
            avoiding=AvoidingRelease(
                version="2.0.0",
                release_type=ReleaseType.MAJOR,
                published_at=pendulum.datetime(2026, 1, 1, tz="UTC"),
                cooldown_days=30,
            ),
        )

    def test_no_state_file_exits_quietly(self, pypi_project: Path) -> None:
        result = CliRunner().invoke(cli, ["reset", "--root", str(pypi_project), "--yes"])
        assert result.exit_code == 0
        assert "nothing to reset" in result.stdout

    def test_prompts_and_aborts_when_user_says_no(self, pypi_project: Path) -> None:
        from chill_out.state import STATE_FILENAME, ChillOutState

        ChillOutState.empty().save(pypi_project)
        result = CliRunner().invoke(cli, ["reset", "--root", str(pypi_project)], input="n\n")
        assert result.exit_code == 0
        assert "Aborted" in result.stdout
        assert (pypi_project / STATE_FILENAME).is_file()  # untouched

    def test_yes_skips_prompt_and_deletes_file_with_no_pins(self, pypi_project: Path) -> None:
        from chill_out.state import STATE_FILENAME, ChillOutState

        ChillOutState.empty().save(pypi_project)
        result = CliRunner().invoke(cli, ["reset", "--root", str(pypi_project), "--yes"])
        assert result.exit_code == 0
        assert "Deleted" in result.stdout
        assert not (pypi_project / STATE_FILENAME).is_file()

    def test_rollback_default_removes_pins_then_deletes_file(self, pypi_project: Path) -> None:
        from chill_out.state import STATE_FILENAME, ChillOutState, RemovalOutcome

        state = ChillOutState.empty()
        state.managed_pins.append(self._make_pin())
        state.save(pypi_project)

        with (
            patch(
                "chill_out.ecosystems.pypi.backend.PypiEcosystem.remove_managed_pin",
                return_value=RemovalOutcome.REMOVED,
            ),
            patch(
                "chill_out.ecosystems.pypi.backend.PypiEcosystem.regenerate_lockfile",
                return_value="regenerated uv.lock",
            ),
        ):
            result = CliRunner().invoke(cli, ["reset", "--root", str(pypi_project), "--yes"])
        assert result.exit_code == 0
        assert "Rolling back" in result.stdout
        assert "removed lodash" in result.stdout
        assert "regenerated uv.lock" in result.stdout
        assert not (pypi_project / STATE_FILENAME).is_file()

    def test_no_rollback_skips_pin_removal_and_deletes_file(self, pypi_project: Path) -> None:
        from chill_out.state import STATE_FILENAME, ChillOutState

        state = ChillOutState.empty()
        state.managed_pins.append(self._make_pin())
        state.save(pypi_project)

        # If the ecosystem were touched this patch would explode; the assertion is that it isn't.
        with patch(
            "chill_out.ecosystems.pypi.backend.PypiEcosystem.remove_managed_pin",
            side_effect=AssertionError("rollback should have been skipped"),
        ):
            result = CliRunner().invoke(cli, ["reset", "--root", str(pypi_project), "--yes", "--no-rollback"])
        assert result.exit_code == 0
        assert "Rolling back" not in result.stdout
        assert not (pypi_project / STATE_FILENAME).is_file()

    def test_dry_run_changes_nothing(self, pypi_project: Path) -> None:
        from chill_out.state import STATE_FILENAME, ChillOutState

        state = ChillOutState.empty()
        state.managed_pins.append(self._make_pin())
        state.save(pypi_project)

        with patch(
            "chill_out.ecosystems.pypi.backend.PypiEcosystem.remove_managed_pin",
            side_effect=AssertionError("dry run should not call remove_managed_pin"),
        ):
            result = CliRunner().invoke(cli, ["reset", "--root", str(pypi_project), "--dry-run"])
        assert result.exit_code == 0
        assert "Would attempt to roll back" in result.stdout
        assert "would remove lodash" in result.stdout
        assert "Would delete" in result.stdout
        assert (pypi_project / STATE_FILENAME).is_file()  # still there

    def test_corrupt_state_file_skips_rollback_and_deletes_anyway(self, pypi_project: Path) -> None:
        """When the state file is unreadable, rollback is skipped with a warning and the file is still deleted."""
        from chill_out.state import STATE_FILENAME

        (pypi_project / STATE_FILENAME).write_text("{not valid json")
        result = CliRunner().invoke(cli, ["reset", "--root", str(pypi_project), "--yes"])
        assert result.exit_code == 0
        assert "Skipping rollback" in result.stdout
        assert not (pypi_project / STATE_FILENAME).is_file()

    def test_rollback_with_drift_and_orphan_renders_diagnostics(self, pypi_project: Path) -> None:
        from chill_out.state import STATE_FILENAME, ChillOutState, RemovalOutcome

        state = ChillOutState.empty()
        state.managed_pins.append(self._make_pin("a"))
        state.managed_pins.append(self._make_pin("b"))
        state.save(pypi_project)

        outcomes = iter([RemovalOutcome.DRIFTED, RemovalOutcome.ORPHAN])
        with patch(
            "chill_out.ecosystems.pypi.backend.PypiEcosystem.remove_managed_pin",
            side_effect=lambda _pin: next(outcomes),
        ):
            result = CliRunner().invoke(cli, ["reset", "--root", str(pypi_project), "--yes"])
        assert result.exit_code == 0
        assert "drift:" in result.stdout
        assert "orphan:" in result.stdout
        assert not (pypi_project / STATE_FILENAME).is_file()

    def test_ecosystem_detection_failure_skips_rollback(self, pypi_project: Path) -> None:
        """If the ecosystem can't be detected, rollback is skipped with a warning and the file is still deleted."""
        from chill_out.exceptions import EcosystemError
        from chill_out.state import STATE_FILENAME, ChillOutState

        state = ChillOutState.empty()
        state.managed_pins.append(self._make_pin())
        state.save(pypi_project)

        with patch(
            "chill_out.cli.main.detect_ecosystem",
            side_effect=EcosystemError("no ecosystem here"),
        ):
            result = CliRunner().invoke(cli, ["reset", "--root", str(pypi_project), "--yes"])
        assert result.exit_code == 0
        assert "Skipping rollback" in result.stdout
        assert "could not detect ecosystem" in result.stdout
        assert not (pypi_project / STATE_FILENAME).is_file()

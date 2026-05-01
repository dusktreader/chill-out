"""
Command-line interface for chill-out.
"""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from snick import unwrap

from chill_out.config import load_config
from chill_out.constants import EcosystemKind, ExitCode, FixStyle
from chill_out.ecosystems import detect_ecosystem, get_ecosystem
from chill_out.exceptions import ChillOutError, handle_errors
from chill_out.render import (
    render_audit_report,
    render_fix_style,
    render_include_groups,
    render_report,
    render_thresholds,
)
from chill_out.runner import audit_async, build_managed_pins, check_async, cleanup_managed_pins, plan_fixes_async
from chill_out.state import STATE_FILENAME, ChillOutState, StateError
from chill_out.version import get_version


def _make_console() -> Console:
    """
    Build the output console.

    When stdout isn't a real terminal (CI runs, captured output, piped output),
    Rich falls back to an 80-column width which truncates the strategy column.
    Force a wider canvas in that case so the strategy tree stays legible.
    Real terminals get their actual width.
    """
    import sys

    if sys.stdout.isatty():
        return Console()
    return Console(width=140)


cli = typer.Typer(
    name="chill-out",
    help="Manage cooldown for package dependencies to avoid zero-day supply chain vulnerabilities.",
    no_args_is_help=True,
    add_completion=False,
)


@cli.command()
def version() -> None:
    """Print the installed chill-out version."""
    typer.echo(get_version())


@cli.command()
@handle_errors("check failed")
def check(
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            "-r",
            help="Project root directory.",
            file_okay=False,
            dir_okay=True,
            exists=True,
        ),
    ] = Path.cwd(),
    ecosystem: Annotated[
        EcosystemKind | None,
        typer.Option("--ecosystem", "-e", help="Force a specific ecosystem; auto-detected otherwise."),
    ] = None,
    fast: Annotated[
        bool,
        typer.Option("--fast", help="Skip the safe-version lookup (faster, but no fix suggestions)."),
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Suppress threshold table.")] = False,
) -> None:
    """Audit every package in the lockfile against the configured cooldown windows (read-only)."""
    import asyncio

    import httpx

    from chill_out.constants import DEFAULT_TIMEOUT

    console = _make_console()
    eco = get_ecosystem(ecosystem, root) if ecosystem else detect_ecosystem(root)
    config = load_config(root, eco.kind)

    if not quiet:
        console.print(render_thresholds(config))
        console.print(render_include_groups(config))
        console.print()

    console.print(f"Checking [bold]{eco.kind.value}[/bold] project at [dim]{root}[/dim]")

    progress, on_start, on_progress = _build_progress(console)

    async def _run():
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as http:
            return await check_async(
                eco,
                config=config,
                fast=fast,
                http=http,
                on_start=on_start,
                on_progress=on_progress,
            )

    with progress:
        report = asyncio.run(_run())
    console.print(render_report(report, config=config, parser=eco.parse_version, fast=fast))

    if report.has_violations:
        raise typer.Exit(code=int(ExitCode.COOLDOWN_VIOLATION))


@cli.command()
@handle_errors("fix failed")
def fix(
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            "-r",
            help="Project root directory.",
            file_okay=False,
            dir_okay=True,
            exists=True,
        ),
    ] = Path.cwd(),
    ecosystem: Annotated[
        EcosystemKind | None,
        typer.Option("--ecosystem", "-e", help="Force a specific ecosystem; auto-detected otherwise."),
    ] = None,
    fix_style: Annotated[
        FixStyle | None,
        typer.Option(
            "--fix-style",
            help=unwrap(
                """
                Override the configured fix_style for this run. 'exact' pins to
                the safe version (pkg==X.Y.Z / X.Y.Z); 'compatible' writes a
                range that admits the safe version's major (>=X.Y.Z,<M+1.0.0 /
                ^X.Y.Z).
                """
            ),
        ),
    ] = None,
    recheck: Annotated[
        bool,
        typer.Option(
            "--recheck/--no-recheck",
            help="After applying fixes, re-run the check to confirm they took.",
        ),
    ] = True,
    cleanup: Annotated[
        bool,
        typer.Option(
            "--cleanup/--no-cleanup",
            help=unwrap(
                """
                Before computing fresh fixes, remove any pins chill-out wrote on previous
                runs whose underlying release has now cleared cooldown. Tracked via the
                project's .chill-out-state.json file.
                """
            ),
        ),
    ] = True,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Suppress threshold table.")] = False,
) -> None:
    """Auto-fix cooldown violations by rewriting manifests/lockfiles to safe versions."""
    import asyncio

    import httpx

    from chill_out.constants import DEFAULT_TIMEOUT
    from chill_out.models import AppliedFixes, CheckReport, FixPlan

    console = _make_console()
    eco = get_ecosystem(ecosystem, root) if ecosystem else detect_ecosystem(root)
    config = load_config(root, eco.kind)
    if fix_style is not None:
        # CLI override beats every config layer for this single run.
        config = config.model_copy(update={"fix_style": fix_style})

    if not quiet:
        console.print(render_thresholds(config))
        console.print(render_include_groups(config))
        console.print()

    state = ChillOutState.load(root)
    if cleanup and state.managed_pins:
        console.print(f"Cleaning up [bold]{len(state.managed_pins)}[/bold] previously-managed pin(s)...")
        cleanup_report = cleanup_managed_pins(eco, state)
        for pin in cleanup_report.removed:
            console.print(f"  [dim]removed {pin.package} from {pin.manifest_path}[/dim]")
        for pin in cleanup_report.drifted:
            console.print(
                "  "
                + unwrap(
                    f"""
                    [yellow]drift: {pin.package} in {pin.manifest_path}
                    no longer matches {pin.pinned_spec!r}; leaving in place and dropping from state.[/yellow]
                    """
                )
            )
        for pin in cleanup_report.orphan:
            console.print(f"  [dim]orphan: {pin.package} no longer present; dropped from state.[/dim]")
        if cleanup_report.removed:
            log_line = eco.regenerate_lockfile()
            console.print(f"  [dim]{log_line}[/dim]")

    console.print(f"Checking [bold]{eco.kind.value}[/bold] project at [dim]{root}[/dim]")

    progress, on_start, on_progress = _build_progress(console)

    async def _check_once(http: httpx.AsyncClient, *, plan_fixes: bool) -> tuple[CheckReport, FixPlan | None]:
        report = await check_async(
            eco,
            config=config,
            fast=False,
            http=http,
            on_start=on_start,
            on_progress=on_progress,
        )
        plan: FixPlan | None = None
        if plan_fixes and report.has_violations:
            plan = await plan_fixes_async(report, eco, config=config, http=http)
        return report, plan

    async def _run() -> tuple[CheckReport, FixPlan | None]:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as http:
            return await _check_once(http, plan_fixes=True)

    with progress:
        report, plan = asyncio.run(_run())
    console.print(render_report(report, config=config, parser=eco.parse_version, fast=False))

    fix_applied = False
    original_violations = list(report.violations)
    applied_runs: list[AppliedFixes] = []
    if report.has_violations and plan is not None:
        if plan.unfixable:
            console.print(f"[yellow]{len(plan.unfixable)} violation(s) cannot be auto-fixed:[/yellow]")
            for entry in plan.unfixable:
                console.print(f"  [yellow]- {entry.violation.name}=={entry.violation.version}:[/yellow] {entry.reason}")
        if not plan.actions:
            console.print("[yellow]No fixable violations.[/yellow]")
        else:
            console.print(f"[bold]Applying {len(plan.actions)} fix action(s)...[/bold]")
            applied = eco.apply_fixes(plan.actions)
            applied_runs.append(applied)
            for line in applied.log:
                console.print(f"  [dim]{line}[/dim]")
            fix_applied = True

    if fix_applied and recheck:
        console.print()
        console.print(f"Re-checking [bold]{eco.kind.value}[/bold] project to verify fixes...")

        async def _recheck() -> CheckReport:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as http:
                report2, _ = await _check_once(http, plan_fixes=False)
                return report2

        with progress:
            report = asyncio.run(_recheck())
        console.print(render_report(report, config=config, parser=eco.parse_version, fast=False))

        # If the direct pin didn't dislodge a violating version (typical of
        # npm's hoisting + sticky lockfile behavior), retry via the
        # ecosystem's override mechanism. Only target violations that we
        # actually attempted to fix on this run; anything new isn't ours to
        # second-guess.
        if report.has_violations and plan is not None:
            attempted = {action.package: action.version for action in plan.actions}
            stuck_actions = [
                action
                for action in plan.actions
                if any(v.name == action.package and v.safe_version is not None for v in report.violations)
            ]
            if stuck_actions and eco.supports_overrides():
                console.print()
                console.print(
                    unwrap(
                        f"""
                        [yellow]{len(stuck_actions)} pin(s) didn't take.
                        Falling back to {eco.kind.value} overrides...[/yellow]
                        """
                    )
                )
                override_result = eco.apply_override_fixes(stuck_actions)
                if override_result is None:  # pragma: no cover - both shipping ecosystems implement overrides
                    console.print(f"[yellow]Override fallback unavailable for {eco.kind.value}.[/yellow]")
                else:
                    applied_runs.append(override_result)
                    for line in override_result.log:
                        console.print(f"  [dim]{line}[/dim]")
                    console.print()
                    console.print("Re-checking after override fallback...")
                    with progress:
                        report = asyncio.run(_recheck())
                    console.print(render_report(report, config=config, parser=eco.parse_version, fast=False))
                    surviving = [v for v in report.violations if v.name in attempted and v.safe_version is not None]
                    if surviving:
                        console.print(
                            f"[red]{len(surviving)} violation(s) survived both direct pin and overrides:[/red]"
                        )
                        for v in surviving:
                            console.print(
                                "  "
                                + unwrap(
                                    f"""
                                    [red]- {v.name}=={v.version}[/red]: the resolver still picked this version
                                    after a direct pin and an override. This usually means another tool or lockfile
                                    rule is forcing it. Try removing the lockfile and reinstalling, or pin the
                                    violating ancestor by hand.
                                    """
                                )
                            )
    elif fix_applied:
        console.print("[green]Fix complete. Re-run `chill-out check` to verify.[/green]")

    new_pins = []
    for run in applied_runs:
        new_pins.extend(build_managed_pins(run, original_violations, config))
    state.managed_pins.extend(new_pins)
    if state.managed_pins:
        state.save(root)
    else:
        state.delete(root)

    if report.has_violations:
        raise typer.Exit(code=int(ExitCode.COOLDOWN_VIOLATION))


@cli.command()
@handle_errors("audit failed")
def audit(
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            "-r",
            help="Project root directory.",
            file_okay=False,
            dir_okay=True,
            exists=True,
        ),
    ] = Path.cwd(),
    ecosystem: Annotated[
        EcosystemKind | None,
        typer.Option("--ecosystem", "-e", help="Force a specific ecosystem; auto-detected otherwise."),
    ] = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Suppress threshold table.")] = False,
) -> None:
    """
    Inspect the state file and report which managed pins are still earning their keep.

    For every entry in `.chill-out-state.json`, the audit asks the registry whether the avoided release has cleared
    its cooldown window or been pulled outright, then buckets the result into stale (pin can be retired), yanked
    (pin can be retired with extra confidence), fresh (still in cooldown -- pin doing its job), or unknown (registry
    couldn't classify -- review manually).

    Read-only: nothing on disk is touched. Run `chill-out fix` (or `chill-out fix --cleanup`, which is the default)
    to actually retire stale and yanked pins.

    Exits 0 when every pin is fresh, exits 7 (STATE_STALE) when any pin is stale or yanked.
    """
    import asyncio

    import httpx

    from chill_out.constants import DEFAULT_TIMEOUT

    console = _make_console()
    state_path = root / STATE_FILENAME
    if not state_path.is_file():
        console.print(f"[dim]No state file at {state_path}; nothing to audit.[/dim]")
        return

    state = ChillOutState.load(root)
    if not state.managed_pins:
        # An on-disk state file with zero pins is unusual (chill-out deletes
        # the file when its pin list empties out), but treat it the same as
        # an absent file rather than blowing up.
        console.print(f"[dim]State file at {state_path} carries no managed pins; nothing to audit.[/dim]")
        return

    eco = get_ecosystem(ecosystem, root) if ecosystem else detect_ecosystem(root)
    config = load_config(root, eco.kind)

    if not quiet:
        console.print(render_thresholds(config))
        console.print(render_include_groups(config))
        console.print()

    console.print(
        f"Auditing [bold]{len(state.managed_pins)}[/bold] managed pin(s) "
        f"for [bold]{eco.kind.value}[/bold] project at [dim]{root}[/dim]"
    )

    async def _run():
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as http:
            return await audit_async(state, eco, config=config, http=http)

    report = asyncio.run(_run())
    console.print(render_audit_report(report))

    if report.has_actionable:
        raise typer.Exit(code=int(ExitCode.STATE_STALE))


def _build_progress(console: Console):
    """Build a transient Progress and return (progress, on_start, on_progress) callbacks."""
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TaskID, TextColumn

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
        transient=True,
    )
    task_id: TaskID | None = None

    def _on_start(packages: list) -> None:
        nonlocal task_id
        task_id = progress.add_task("Checking registry...", total=len(packages))

    def _on_progress(_pkg) -> None:
        if task_id is not None:
            progress.advance(task_id)

    return progress, _on_start, _on_progress


@cli.command()
@handle_errors("show-config failed")
def show_config(
    root: Annotated[Path, typer.Option("--root", "-r", exists=True, file_okay=False)] = Path.cwd(),
    ecosystem: Annotated[
        EcosystemKind | None,
        typer.Option("--ecosystem", "-e", help="Ecosystem to resolve config for."),
    ] = None,
) -> None:
    """Print the resolved cooldown configuration for the project."""
    console = _make_console()
    eco = get_ecosystem(ecosystem, root) if ecosystem else detect_ecosystem(root)
    config = load_config(root, eco.kind)
    console.print(f"Resolved config for [bold]{eco.kind.value}[/bold] at [dim]{root}[/dim]")
    console.print(render_thresholds(config))
    console.print(render_include_groups(config))
    console.print(render_fix_style(config))


@cli.command()
@handle_errors("reset failed")
def reset(
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            "-r",
            help="Project root directory.",
            file_okay=False,
            dir_okay=True,
            exists=True,
        ),
    ] = Path.cwd(),
    ecosystem: Annotated[
        EcosystemKind | None,
        typer.Option(
            "--ecosystem",
            "-e",
            help="Force a specific ecosystem for rollback; auto-detected otherwise.",
        ),
    ] = None,
    rollback: Annotated[
        bool,
        typer.Option(
            "--rollback/--no-rollback",
            help=unwrap(
                """
                Before deleting the state file, try to remove every pin chill-out wrote into
                the project's manifests. Defaults on. Pass --no-rollback to leave the pins in
                place and only forget about them.
                """
            ),
        ),
    ] = True,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirmation prompt."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Report what would happen without changing anything on disk.",
        ),
    ] = False,
) -> None:
    """
    Forget every pin chill-out is tracking and delete the state file.

    Useful as an escape hatch when the state file gets corrupted, when you've decided you no
    longer want chill-out managing your project, or when you want to start the bookkeeping
    over from scratch. By default chill-out also tries to roll back its pins from the project's
    manifests so you don't end up with orphaned entries; pass --no-rollback to skip that and
    only delete the state file.

    Rollback is best-effort. If the state file is unreadable or the ecosystem can't be detected,
    the rollback step is skipped with a warning and the state file is deleted anyway. The state
    file delete is the one operation reset is contractually required to perform.
    """
    console = _make_console()
    state_path = root / STATE_FILENAME

    if not state_path.is_file():
        console.print(f"[dim]No state file at {state_path}; nothing to reset.[/dim]")
        return

    if not yes and not dry_run:
        rollback_clause = " and try to roll back any pins chill-out wrote." if rollback else "."
        confirmed = typer.confirm(
            unwrap(
                f"""
                This will delete {state_path}{rollback_clause}
                Continue?
                """
            ),
            default=False,
        )
        if not confirmed:
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=int(ExitCode.SUCCESS))

    pins_to_rollback: list = []
    rollback_skipped_reason: str | None = None
    state: ChillOutState | None = None
    if rollback:
        try:
            state = ChillOutState.load(root)
            pins_to_rollback = list(state.managed_pins)
        except StateError as exc:
            rollback_skipped_reason = f"state file unreadable: {exc}"

    if rollback and rollback_skipped_reason is None and pins_to_rollback:
        try:
            eco = get_ecosystem(ecosystem, root) if ecosystem else detect_ecosystem(root)
        except ChillOutError as exc:
            rollback_skipped_reason = f"could not detect ecosystem: {exc}"
            eco = None
        if eco is not None:
            assert state is not None  # narrow for type-checker; load() succeeded above
            if dry_run:
                console.print(
                    unwrap(
                        f"""
                        [dim]Would attempt to roll back {len(pins_to_rollback)} pin(s)
                        via the {eco.kind.value} ecosystem.[/dim]
                        """
                    )
                )
                for pin in pins_to_rollback:
                    console.print(f"  [dim]would remove {pin.package} from {pin.manifest_path}[/dim]")
            else:
                console.print(f"Rolling back [bold]{len(pins_to_rollback)}[/bold] managed pin(s)...")
                report = cleanup_managed_pins(eco, state)
                for pin in report.removed:
                    console.print(f"  [dim]removed {pin.package} from {pin.manifest_path}[/dim]")
                for pin in report.drifted:
                    console.print(
                        "  "
                        + unwrap(
                            f"""
                            [yellow]drift: {pin.package} in {pin.manifest_path}
                            no longer matches {pin.pinned_spec!r}; left in place.[/yellow]
                            """
                        )
                    )
                for pin in report.orphan:
                    console.print(f"  [dim]orphan: {pin.package} no longer present.[/dim]")
                if report.removed:
                    log_line = eco.regenerate_lockfile()
                    console.print(f"  [dim]{log_line}[/dim]")

    if rollback and rollback_skipped_reason is not None:
        console.print(
            unwrap(
                f"""
                [yellow]Skipping rollback ({rollback_skipped_reason}).
                Deleting the state file anyway.[/yellow]
                """
            )
        )

    if dry_run:
        console.print(f"[dim]Would delete {state_path}.[/dim]")
        return

    state_path.unlink()
    console.print(f"[green]Deleted {state_path}. chill-out is no longer tracking any pins.[/green]")

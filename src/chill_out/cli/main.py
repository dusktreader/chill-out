"""
Command-line interface for chill-out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from chill_out.config import load_config
from chill_out.constants import EcosystemKind, ExitCode
from chill_out.ecosystems import detect_ecosystem, get_ecosystem
from chill_out.exceptions import handle_errors
from chill_out.reporting import render_include_groups, render_report, render_thresholds
from chill_out.runner import check_async, plan_fixes_async
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
    deep: Annotated[bool, typer.Option("--deep", help="Include transitive dependencies in the check.")] = False,
    fast: Annotated[
        bool,
        typer.Option("--fast", help="Skip the safe-version lookup (faster, but no fix suggestions)."),
    ] = False,
    fix: Annotated[
        bool,
        typer.Option(
            "--fix",
            help="Apply fix actions for any violation that has a known safe version.",
        ),
    ] = False,
    recheck: Annotated[
        bool,
        typer.Option(
            "--recheck/--no-recheck",
            help="After --fix, re-run the check to confirm the fix worked. Has no effect without --fix.",
        ),
    ] = True,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Suppress threshold table.")] = False,
) -> None:
    """Check installed packages against the configured cooldown windows."""
    import asyncio

    import httpx
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TaskID, TextColumn

    from chill_out.constants import DEFAULT_TIMEOUT
    from chill_out.models import CheckReport, FixPlan

    if fix and fast:
        raise typer.BadParameter("--fix requires safe-version lookup; cannot be combined with --fast")

    console = _make_console()
    eco = get_ecosystem(ecosystem, root) if ecosystem else detect_ecosystem(root)
    config = load_config(root, eco.kind)

    if not quiet:
        render_thresholds(config, console)
        render_include_groups(config, console)
        console.print()

    console.print(f"Checking [bold]{eco.kind.value}[/bold] project at [dim]{root}[/dim]")

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

    async def _check_once(http: httpx.AsyncClient, *, plan_fixes: bool) -> tuple[CheckReport, FixPlan | None]:
        nonlocal task_id
        task_id = None
        report = await check_async(
            eco,
            config=config,
            deep=deep,
            fast=fast,
            http=http,
            on_start=_on_start,
            on_progress=_on_progress,
        )
        plan: FixPlan | None = None
        if plan_fixes and report.has_violations:
            plan = await plan_fixes_async(report, eco, config=config, http=http)
        return report, plan

    async def _run() -> tuple[CheckReport, FixPlan | None]:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as http:
            return await _check_once(http, plan_fixes=fix)

    with progress:
        report, plan = asyncio.run(_run())
    render_report(report, console, config=config, fast=fast)

    fix_applied = False
    if fix and report.has_violations and plan is not None:
        if plan.unfixable:
            console.print(f"[yellow]{len(plan.unfixable)} violation(s) cannot be auto-fixed:[/yellow]")
            for entry in plan.unfixable:
                console.print(f"  [yellow]- {entry.violation.name}=={entry.violation.version}:[/yellow] {entry.reason}")
        if not plan.actions:
            console.print("[yellow]No fixable violations.[/yellow]")
        else:
            console.print(f"[bold]Applying {len(plan.actions)} fix action(s)...[/bold]")
            log = eco.apply_fixes(plan.actions)
            for line in log:
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
        render_report(report, console, config=config, fast=fast)

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
                    f"[yellow]{len(stuck_actions)} pin(s) didn't take. "
                    f"Falling back to {eco.kind.value} overrides...[/yellow]"
                )
                override_log = eco.apply_override_fixes(stuck_actions)
                if override_log is None:
                    console.print(f"[yellow]Override fallback unavailable for {eco.kind.value}.[/yellow]")
                else:
                    for line in override_log:
                        console.print(f"  [dim]{line}[/dim]")
                    console.print()
                    console.print("Re-checking after override fallback...")
                    with progress:
                        report = asyncio.run(_recheck())
                    render_report(report, console, config=config, fast=fast)
                    surviving = [
                        v
                        for v in report.violations
                        if v.name in attempted and v.safe_version is not None
                    ]
                    if surviving:
                        console.print(
                            f"[red]{len(surviving)} violation(s) survived both direct pin and overrides:[/red]"
                        )
                        for v in surviving:
                            console.print(
                                f"  [red]- {v.name}=={v.version}[/red]: the resolver still picked this version "
                                "after a direct pin and an override. This usually means another tool or lockfile "
                                "rule is forcing it. Try removing the lockfile and reinstalling, or pin the "
                                "violating ancestor by hand."
                            )
    elif fix_applied:
        console.print("[green]Fix complete. Re-run `chill-out check` to verify.[/green]")

    if report.has_violations:
        raise typer.Exit(code=int(ExitCode.COOLDOWN_VIOLATION))


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
    render_thresholds(config, console)
    render_include_groups(config, console)

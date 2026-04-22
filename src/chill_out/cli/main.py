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
from chill_out.reporting import render_report, render_thresholds
from chill_out.runner import check_async, plan_fixes
from chill_out.version import get_version

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
    deep: Annotated[
        bool, typer.Option("--deep", help="Include transitive dependencies in the check.")
    ] = False,
    fast: Annotated[
        bool,
        typer.Option(
            "--fast", help="Skip the safe-version lookup (faster, but no fix suggestions)."
        ),
    ] = False,
    fix: Annotated[
        bool,
        typer.Option(
            "--fix",
            help="Apply fix actions for any violation that has a known safe version.",
        ),
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Suppress threshold table.")] = False,
) -> None:
    """Check installed packages against the configured cooldown windows."""
    import asyncio

    if fix and fast:
        raise typer.BadParameter("--fix requires safe-version lookup; cannot be combined with --fast")

    console = Console()
    eco = get_ecosystem(ecosystem, root) if ecosystem else detect_ecosystem(root)
    config = load_config(root, eco.kind)

    if not quiet:
        render_thresholds(config, console)
        console.print()

    console.print(f"Checking [bold]{eco.kind.value}[/bold] project at [dim]{root}[/dim]")
    report = asyncio.run(check_async(eco, config=config, deep=deep, fast=fast))
    render_report(report, console, fast=fast)

    if fix and report.has_violations:
        actions = plan_fixes(report)
        if not actions:
            console.print("[yellow]No fixable violations.[/yellow]")
        else:
            console.print(f"[bold]Applying {len(actions)} fix action(s)...[/bold]")
            log = eco.apply_fixes(actions)
            for line in log:
                console.print(f"  [dim]{line}[/dim]")
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
    console = Console()
    eco = get_ecosystem(ecosystem, root) if ecosystem else detect_ecosystem(root)
    config = load_config(root, eco.kind)
    console.print(f"Resolved config for [bold]{eco.kind.value}[/bold] at [dim]{root}[/dim]")
    render_thresholds(config, console)

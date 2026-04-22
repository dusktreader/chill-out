"""
Rich-based reporting for cooldown check results.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from chill_out.config import CooldownConfig
from chill_out.constants import ReleaseType
from chill_out.models import CheckReport

_RELEASE_COLOR = {
    ReleaseType.MAJOR: "red",
    ReleaseType.MINOR: "yellow",
    ReleaseType.PATCH: "cyan",
    ReleaseType.DEFAULT: "white",
}


def render_thresholds(config: CooldownConfig, console: Console) -> None:
    """Print a small table of the active cooldown thresholds."""
    table = Table(
        title="Cooldown thresholds",
        title_justify="left",
        header_style="bold",
    )
    table.add_column("Release Type")
    table.add_column("Days", justify="right")
    for rel_type in (ReleaseType.PATCH, ReleaseType.MINOR, ReleaseType.MAJOR, ReleaseType.DEFAULT):
        color = _RELEASE_COLOR.get(rel_type, "white")
        table.add_row(f"[{color}]{rel_type.value}[/{color}]", str(config.for_release_type(rel_type)))
    console.print(table)


def render_report(report: CheckReport, console: Console, *, fast: bool = False) -> None:
    """
    Print a summary of the report.

    When there are no violations, prints a single success line and returns.
    """
    if not report.violations:
        console.print(
            f"[green]No cooldown violations across {len(report.checked)} {report.ecosystem.value} package(s).[/green]"
        )
        if report.skipped:
            console.print(f"[dim]({len(report.skipped)} package(s) skipped)[/dim]")
        return

    console.print(
        f"[red]{len(report.violations)} cooldown violation(s) "
        f"in {len(report.checked)} {report.ecosystem.value} package(s):[/red]"
    )

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Package")
    table.add_column("Installed")
    table.add_column("Release Type")
    table.add_column("Age", justify="right")
    table.add_column("Limit", justify="right")
    if not fast:
        table.add_column("Suggested safe version")
    if any(v.via for v in report.violations):
        table.add_column("Via")

    has_via = any(v.via for v in report.violations)

    for v in sorted(report.violations, key=lambda x: x.name):
        rel_color = _RELEASE_COLOR.get(v.release_type, "white")
        row = [
            f"[bold]{v.name}[/bold]",
            v.version,
            f"[{rel_color}]{v.release_type.value}[/{rel_color}]",
            f"{v.age_days}d",
            f"{v.limit_days}d",
        ]
        if not fast:
            if v.safe_version:
                row.append(f"[green]{v.safe_version.version}[/green] ({v.safe_version.age_days}d old)")
            else:
                row.append("[dim]none[/dim]")
        if has_via:
            row.append(v.via or "")
        table.add_row(*row)

    console.print(table)
    if report.skipped:
        console.print(f"[dim]({len(report.skipped)} package(s) skipped)[/dim]")

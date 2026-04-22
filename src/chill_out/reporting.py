"""
Rich-based reporting for cooldown check results.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from chill_out.config import CooldownConfig
from chill_out.constants import ReleaseType
from chill_out.cooldown import release_type
from chill_out.models import CheckReport, InstalledPackage, Violation

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


def _fmt_pkg_label(name: str, version: str | None, rel_type: ReleaseType | None = None) -> str:
    """Render a single package label suitable for a tree node."""
    if version is None:
        return f"[bold]{name}[/bold]"
    color = _RELEASE_COLOR.get(rel_type, "white") if rel_type else "white"
    return f"[bold]{name}[/bold] [{color}]{version}[/{color}]"


def _build_via_tree(
    violation: Violation,
    installed_index: dict[str, InstalledPackage],
) -> Tree:
    """
    Render the dependency chain that pulled the violating package in.

    The shape mirrors the upstream script: the principal sits at the root, each
    intermediate transitive becomes a child node, and the violating package
    itself is the leaf. Intermediate nodes pull their version info from the
    installed-package index so the chain stays grounded in the project's
    actual lockfile.
    """
    chain_top_down = list(reversed(violation.package.via_chain))
    principal_name = chain_top_down[0]
    principal_pkg = installed_index.get(principal_name)
    principal_version = principal_pkg.version if principal_pkg else None
    principal_rel = release_type(principal_version) if principal_version else None
    tree = Tree(_fmt_pkg_label(principal_name, principal_version, principal_rel), guide_style="dim")
    node = tree
    for intermediate in chain_top_down[1:]:
        ipkg = installed_index.get(intermediate)
        iver = ipkg.version if ipkg else None
        irel = release_type(iver) if iver else None
        node = node.add(_fmt_pkg_label(intermediate, iver, irel))
    leaf_color = _RELEASE_COLOR.get(violation.release_type, "white")
    leaf = (
        f"[bold]{violation.name}[/bold] "
        f"[{leaf_color}]{violation.version}[/{leaf_color}] "
        f"[red](age {violation.age_days}d > {violation.limit_days}d)[/red]"
    )
    node.add(leaf)
    return tree


def _fmt_pin(name: str, version: str, age_days: int | None) -> str:
    """Render a 'pin this package to this version' label for the strategy tree."""
    age_str = f" [dim]({age_days}d old)[/dim]" if age_days is not None else ""
    return f"[bold]{name}[/bold] -> [green]{version}[/green]{age_str}"


def _build_strategy(violation: Violation) -> Tree | str:
    """
    Render the recommended fix recipe for a violation.

    For a principal violation, the strategy is a single pin of the violating
    package itself. For a transitive, it's a tree showing the chain from the
    principal down to the transitive, with the leaf labelled as the explicit
    pin to apply. When no safe version is known, returns a dim 'none' marker
    so the column still has something to show.

    This is a display-only summary. The actual fix may also need to roll back
    the principal when the safe transitive version conflicts with whatever
    the principal's range admits; that decision lives in the planner and
    surfaces through ``plan_fixes_async``, not here.
    """
    if violation.safe_version is None:
        return "[dim]no safe version found[/dim]"

    pin_label = _fmt_pin(violation.name, violation.safe_version.version, violation.safe_version.age_days)

    if not violation.package.via_chain:
        return pin_label

    chain_top_down = list(reversed(violation.package.via_chain))
    tree = Tree(f"[dim]{chain_top_down[0]}[/dim]", guide_style="dim")
    node = tree
    for intermediate in chain_top_down[1:]:
        node = node.add(f"[dim]{intermediate}[/dim]")
    node.add(pin_label)
    return tree


def render_report(report: CheckReport, console: Console, *, fast: bool = False) -> None:
    """
    Print a summary of the report.

    When there are no violations, prints a single success line and returns.
    Transitive violations are rendered as a dependency tree so the chain
    that pulled them in is visible at a glance.
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

    has_via = any(v.via for v in report.violations)
    installed_index = {p.name: p for p in report.checked}

    table = Table(show_header=True, header_style="bold cyan", show_lines=has_via)
    table.add_column("Package", min_width=40)
    table.add_column("Release Type")
    table.add_column("Age", justify="right")
    table.add_column("Limit", justify="right")
    if not fast:
        table.add_column("Strategy", min_width=45)

    for v in sorted(report.violations, key=lambda x: x.name):
        rel_color = _RELEASE_COLOR.get(v.release_type, "white")
        if v.via:
            pkg_cell = _build_via_tree(v, installed_index)
        else:
            pkg_cell = _fmt_pkg_label(v.name, v.version, v.release_type)
        row = [
            pkg_cell,
            f"[{rel_color}]{v.release_type.value}[/{rel_color}]",
            f"{v.age_days}d",
            f"{v.limit_days}d",
        ]
        if not fast:
            row.append(_build_strategy(v))
        table.add_row(*row)

    console.print(table)
    if report.skipped:
        console.print(f"[dim]({len(report.skipped)} package(s) skipped)[/dim]")

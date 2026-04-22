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


def _fmt_pkg_node(name: str, version: str | None, age_days: int | None = None) -> str:
    """Render a package label for a non-violating tree node.

    Shows ``name = version`` with an optional age suffix when known. Used for
    principals and intermediates that aren't themselves violating; the report
    doesn't carry their ages, so the suffix is normally omitted for those.
    """
    if version is None:
        return f"[bold]{name}[/bold]"
    age_str = f" [dim]({age_days}d old)[/dim]" if age_days is not None else ""
    return f"[bold]{name}[/bold] = [dim]{version}[/dim]{age_str}"


def _fmt_violating_pkg_node(violation: Violation) -> str:
    """Render the package label for the violating leaf row.

    Uses the release-type color on the version and calls out the age vs limit
    in red so the violation reads at a glance.
    """
    color = _RELEASE_COLOR.get(violation.release_type, "white")
    return (
        f"[bold]{violation.name}[/bold] = [{color}]{violation.version}[/{color}]"
        f" [red](age {violation.age_days}d > {violation.limit_days}d)[/red]"
    )


def _fmt_limit_node(rel_type: ReleaseType, limit_days: int) -> str:
    """Render a limit-column label: release type plus the threshold in days."""
    color = _RELEASE_COLOR.get(rel_type, "white")
    return f"[{color}]{rel_type.value}[/{color}] [dim]{limit_days}d[/dim]"


def _build_pkg_tree(
    violation: Violation,
    installed_index: dict[str, InstalledPackage],
) -> Tree:
    """Render the dependency chain that pulled the violating package in.

    Principal at the root, intermediates as children, the violating package
    itself as the leaf with its age vs limit called out.
    """
    chain_top_down = list(reversed(violation.package.via_chain))
    principal_name = chain_top_down[0]
    principal_pkg = installed_index.get(principal_name)
    principal_version = principal_pkg.version if principal_pkg else None
    tree = Tree(_fmt_pkg_node(principal_name, principal_version), guide_style="dim")
    node = tree
    for intermediate in chain_top_down[1:]:
        ipkg = installed_index.get(intermediate)
        iver = ipkg.version if ipkg else None
        node = node.add(_fmt_pkg_node(intermediate, iver))
    node.add(_fmt_violating_pkg_node(violation))
    return tree


def _build_limit_tree(
    violation: Violation,
    installed_index: dict[str, InstalledPackage],
    config: CooldownConfig,
) -> Tree:
    """Render a parallel tree for the limit column, mirroring the package tree.

    Each node shows the release type and threshold of the corresponding
    package in the chain. The leaf shows the violation's own release type
    and limit; non-violating ancestors get their values from
    ``release_type(version)`` and the cooldown config.
    """
    chain_top_down = list(reversed(violation.package.via_chain))

    def node_label_for(name: str) -> str:
        pkg = installed_index.get(name)
        if pkg is None:
            return "[dim]-[/dim]"
        rel = release_type(pkg.version)
        return _fmt_limit_node(rel, config.for_release_type(rel))

    tree = Tree(node_label_for(chain_top_down[0]), guide_style="dim")
    node = tree
    for intermediate in chain_top_down[1:]:
        node = node.add(node_label_for(intermediate))
    node.add(_fmt_limit_node(violation.release_type, violation.limit_days))
    return tree


def _fmt_pin(name: str, version: str, age_days: int | None) -> str:
    """Render a 'pin this package to this version' label for the strategy tree."""
    age_str = f" [dim]({age_days}d old)[/dim]" if age_days is not None else ""
    return f"[bold]{name}[/bold] -> [green]{version}[/green]{age_str}"


def _build_strategy(violation: Violation) -> Tree | str:
    """Render the recommended fix recipe for a violation.

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


def render_report(
    report: CheckReport,
    console: Console,
    *,
    config: CooldownConfig,
    fast: bool = False,
) -> None:
    """Print a summary of the report.

    When there are no violations, prints a single success line and returns.
    Otherwise renders one row per violation with three columns:

    - **Package**: ``name = version (<n>d old)`` for principals, or a tree from
      principal down to the violating leaf for transitives.
    - **Limit**: a parallel tree showing each chain member's release type and
      threshold, so the reader can see why the leaf tripped.
    - **Strategy**: the explicit fix recipe (omitted under ``--fast``).
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
    table.add_column("Limit")
    if not fast:
        table.add_column("Strategy", min_width=45)

    for v in sorted(report.violations, key=lambda x: x.name):
        if v.via:
            pkg_cell: Tree | str = _build_pkg_tree(v, installed_index)
            limit_cell: Tree | str = _build_limit_tree(v, installed_index, config)
        else:
            pkg_cell = _fmt_violating_pkg_node(v)
            limit_cell = _fmt_limit_node(v.release_type, v.limit_days)
        row: list[Tree | str] = [pkg_cell, limit_cell]
        if not fast:
            row.append(_build_strategy(v))
        table.add_row(*row)

    console.print(table)
    if report.skipped:
        console.print(f"[dim]({len(report.skipped)} package(s) skipped)[/dim]")

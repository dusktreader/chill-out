"""
Rich-based reporting for cooldown check results.

Every `render_*` function in this module returns a `RenderableType` rather than printing
directly. The CLI does the actual `console.print(...)` calls. Keeping rendering pure makes
each helper independently composable and trivially testable: capture the renderable, print it
into a `Console(file=StringIO())`, inspect the bytes.
"""

from rich.console import Group, RenderableType
from rich.table import Table
from rich.tree import Tree

from chill_out.config import ChillOutConfig
from chill_out.constants import AuditStatus, DependencyGroup, ReleaseType
from chill_out.cooldown import release_type
from chill_out.ecosystems.version_parsing import VersionParser
from chill_out.models import AuditedPin, AuditReport, CheckReport, InstalledPackage, Violation

RELEASE_COLOR = {
    ReleaseType.MAJOR: "red",
    ReleaseType.MINOR: "yellow",
    ReleaseType.PATCH: "cyan",
    ReleaseType.DEFAULT: "white",
}


def render_thresholds(config: ChillOutConfig) -> RenderableType:
    """Render a small table of the active cooldown thresholds."""
    table = Table(
        title="Cooldown thresholds",
        title_justify="left",
        header_style="bold",
    )
    table.add_column("Release Type")
    table.add_column("Days", justify="right")
    for rel_type in (ReleaseType.PATCH, ReleaseType.MINOR, ReleaseType.MAJOR, ReleaseType.DEFAULT):
        color = RELEASE_COLOR.get(rel_type, "white")
        table.add_row(f"[{color}]{rel_type.value}[/{color}]", str(config.for_release_type(rel_type)))
    return table


def render_include_groups(config: ChillOutConfig) -> RenderableType:
    """
    Render the configured `include_groups` as a single-line label.

    Empty configurations are rendered explicitly so it's obvious that nothing will be checked,
    rather than the line being silently dropped.
    """
    if not config.include_groups:
        return "[yellow]Included groups: (none -- nothing will be checked)[/yellow]"
    label = ", ".join(g.value for g in config.include_groups)
    return f"Included groups: [bold]{label}[/bold]"


def render_fix_style(config: ChillOutConfig) -> RenderableType:
    """Render the configured `fix_style` as a single-line label."""
    return f"Fix style: [bold]{config.fix_style.value}[/bold]"


def format_groups(groups: tuple[DependencyGroup, ...]) -> str:
    """
    Render a compact `[group, group]` suffix, or empty when nothing to show.

    The leading space is included so callers can always concatenate the return value without
    conditionally inserting separators.
    """
    if not groups:
        return ""
    label = ", ".join(g.value for g in groups)
    return f" [dim]\\[{label}][/dim]"


def format_package_node(name: str, version: str | None, age_days: int | None = None) -> str:
    """
    Render a package label for a non-violating tree node.

    Shows `name = version` with an optional age suffix when known. Used for principals and
    intermediates that aren't themselves violating; the report doesn't carry their ages, so
    the suffix is normally omitted for those.
    """
    if version is None:
        return f"[bold]{name}[/bold]"
    age_str = f" [dim]({age_days}d old)[/dim]" if age_days is not None else ""
    return f"[bold]{name}[/bold] = [dim]{version}[/dim]{age_str}"


def format_violating_package_node(violation: Violation) -> str:
    """
    Render the package label for the violating leaf row.

    Uses the release-type color on the version and calls out the age vs limit in red so the
    violation reads at a glance. Includes the package's group membership when known so the
    reader can spot at a glance whether the violation is in a main, dev, or optional dependency.
    """
    color = RELEASE_COLOR.get(violation.release_type, "white")
    return (
        f"[bold]{violation.name}[/bold] = [{color}]{violation.version}[/{color}]"
        f" [red](age {violation.age_days}d > {violation.limit_days}d)[/red]"
        f"{format_groups(violation.package.groups)}"
    )


def format_limit_node(rel_type: ReleaseType, limit_days: int) -> str:
    """Render a limit-column label: release type plus the threshold in days."""
    color = RELEASE_COLOR.get(rel_type, "white")
    return f"[{color}]{rel_type.value}[/{color}] [dim]{limit_days}d[/dim]"


def render_package_tree(
    violation: Violation,
    installed_index: dict[str, InstalledPackage],
) -> Tree:
    """
    Render the dependency chain that pulled the violating package in.

    Principal at the root, intermediates as children, the violating package itself as the leaf
    with its age vs limit called out.
    """
    chain_top_down = list(reversed(violation.package.via_chain))
    principal_name = chain_top_down[0]
    principal_pkg = installed_index.get(principal_name)
    principal_version = principal_pkg.version if principal_pkg else None
    principal_groups = principal_pkg.groups if principal_pkg else ()
    principal_label = format_package_node(principal_name, principal_version) + format_groups(principal_groups)
    tree = Tree(principal_label, guide_style="dim")
    node = tree
    for intermediate in chain_top_down[1:]:
        ipkg = installed_index.get(intermediate)
        iver = ipkg.version if ipkg else None
        node = node.add(format_package_node(intermediate, iver))
    node.add(format_violating_package_node(violation))
    return tree


def render_limit_tree(
    violation: Violation,
    installed_index: dict[str, InstalledPackage],
    config: ChillOutConfig,
    parser: VersionParser,
) -> Tree:
    """
    Render a parallel tree for the limit column, mirroring the package tree.

    Each node shows the release type and threshold of the corresponding package in the chain.
    The leaf shows the violation's own release type and limit; non-violating ancestors get
    their values from `release_type(version)` and the cooldown config.
    """
    chain_top_down = list(reversed(violation.package.via_chain))

    def node_label_for(name: str) -> str:
        pkg = installed_index.get(name)
        if pkg is None:
            return "[dim]-[/dim]"
        rel = release_type(pkg.version, parser)
        return format_limit_node(rel, config.for_release_type(rel))

    tree = Tree(node_label_for(chain_top_down[0]), guide_style="dim")
    node = tree
    for intermediate in chain_top_down[1:]:
        node = node.add(node_label_for(intermediate))
    node.add(format_limit_node(violation.release_type, violation.limit_days))
    return tree


def format_pin(name: str, version: str, age_days: int | None) -> str:
    """Render a 'pin this package to this version' label for the strategy tree."""
    age_str = f" [dim]({age_days}d old)[/dim]" if age_days is not None else ""
    return f"[bold]{name}[/bold] -> [green]{version}[/green]{age_str}"


def render_strategy(violation: Violation) -> RenderableType:
    """
    Render the recommended fix recipe for a violation.

    For a principal violation, the strategy is a single pin of the violating package itself.
    For a transitive, it's a tree showing the chain from the principal down to the transitive,
    with the leaf labelled as the explicit pin to apply. When no safe version is known,
    returns a dim 'none' marker so the column still has something to show.

    When the violation is shared across multiple workspace members, the strategy includes an
    annotation listing the members. That signals to the user (and to the fix planner) that a
    member-level pin will leave the sibling-shared copy in place and an override is the right
    move.

    This is a display-only summary. The actual fix may also need to roll back the principal
    when the safe transitive version conflicts with whatever the principal's range admits;
    that decision lives in the planner and surfaces through `plan_fixes_async`, not here.
    """
    if violation.safe_version is None:
        return "[dim]no safe version found[/dim]"

    pin_label = format_pin(violation.name, violation.safe_version.version, violation.safe_version.age_days)
    if violation.is_shared:
        owners = ", ".join(violation.member_owners)
        pin_label = f"{pin_label} [yellow](shared: {owners}; will use overrides)[/yellow]"

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
    *,
    config: ChillOutConfig,
    parser: VersionParser,
    fast: bool = False,
) -> RenderableType:
    """
    Render a summary of the report.

    When there are no violations, returns a single success line (plus a skipped-count tail
    when relevant). Otherwise returns a `Group` containing a headline, a violations table,
    and an optional skipped-count footer. The table has three columns:

    - **Package**: `name = version (<n>d old)` for principals, or a tree from principal down
      to the violating leaf for transitives.
    - **Limit**: a parallel tree showing each chain member's release type and threshold, so
      the reader can see why the leaf tripped.
    - **Strategy**: the explicit fix recipe (omitted under `--fast`).
    """
    if not report.violations:
        headline: RenderableType = (
            f"[green]No cooldown violations across {len(report.checked)} {report.ecosystem.value} package(s).[/green]"
        )
        if report.skipped:
            return Group(headline, f"[dim]({len(report.skipped)} package(s) skipped)[/dim]")
        return headline

    headline = (
        f"[red]{len(report.violations)} cooldown violation(s) "
        f"in {len(report.checked)} {report.ecosystem.value} package(s):[/red]"
    )

    has_via = any(v.via for v in report.violations)
    # Multiple installations can share a name (different versions hoisted at different nesting
    # levels). Group by name and prefer the shallowest entry — typically the one closest to
    # the project root, which is the version display picks for unattributed intermediates in
    # the chain.
    installed_index: dict[str, InstalledPackage] = {}
    for p in report.checked:
        existing = installed_index.get(p.name)
        if existing is None or len(p.via_chain) < len(existing.via_chain):
            installed_index[p.name] = p

    table = Table(show_header=True, header_style="bold cyan", show_lines=has_via)
    table.add_column("Package", min_width=40)
    table.add_column("Limit")
    if not fast:
        table.add_column("Strategy", min_width=45)

    for v in sorted(report.violations, key=lambda x: x.name):
        if v.via:
            pkg_cell: RenderableType = render_package_tree(v, installed_index)
            limit_cell: RenderableType = render_limit_tree(v, installed_index, config, parser)
        else:
            pkg_cell = format_violating_package_node(v)
            limit_cell = format_limit_node(v.release_type, v.limit_days)
        row: list[RenderableType] = [pkg_cell, limit_cell]
        if not fast:
            row.append(render_strategy(v))
        table.add_row(*row)

    if report.skipped:
        return Group(headline, table, f"[dim]({len(report.skipped)} package(s) skipped)[/dim]")
    return Group(headline, table)


AUDIT_STATUS_COLOR: dict[AuditStatus, str] = {
    AuditStatus.FRESH: "green",
    AuditStatus.STALE: "yellow",
    AuditStatus.YANKED: "red",
    AuditStatus.UNKNOWN: "cyan",
}


def _format_age(entry: AuditedPin) -> str:
    """Render the avoided release's current age as `<n>d / <limit>d`, or `?` when unknown."""
    if entry.current_age_days is None:
        return "[dim]?[/dim]"
    return f"{entry.current_age_days}d / {entry.cooldown_days}d"


def _render_audit_bucket(title: str, status: AuditStatus, entries: list[AuditedPin]) -> Table:
    """
    Render one bucket of the audit report as a Table.

    Columns: package, avoided version, release type, age vs cooldown
    threshold, and (for `UNKNOWN`) a detail column carrying the reason
    the audit couldn't classify the entry. The bucket title is
    color-coded by status so the eye can land on actionable rows
    without reading.
    """
    color = AUDIT_STATUS_COLOR[status]
    table = Table(
        title=f"[{color}]{title} ({len(entries)})[/{color}]",
        title_justify="left",
        header_style="bold",
        show_lines=False,
    )
    table.add_column("Package")
    table.add_column("Avoiding")
    table.add_column("Release type")
    table.add_column("Age / threshold", justify="right")
    if status is AuditStatus.UNKNOWN:
        table.add_column("Detail")

    for entry in entries:
        rel_color = RELEASE_COLOR.get(entry.pin.avoiding.release_type, "white")
        row: list[str] = [
            f"[bold]{entry.pin.package}[/bold]",
            entry.pin.avoiding.version,
            f"[{rel_color}]{entry.pin.avoiding.release_type.value}[/{rel_color}]",
            _format_age(entry),
        ]
        if status is AuditStatus.UNKNOWN:
            row.append(f"[dim]{entry.detail or '-'}[/dim]")
        table.add_row(*row)
    return table


def render_audit_report(report: AuditReport) -> RenderableType:
    """
    Render the result of an `audit` run.

    Empty state files are reported as a single success line. Otherwise
    returns a `Group` with a headline summarizing the bucket counts plus
    one table per non-empty bucket: stale and yanked first (these are
    the actionable buckets), fresh next (informational), unknown last
    (the user must decide). Each table is color-coded by status so the
    actionable rows pull the eye.
    """
    if not report.entries:
        return f"[green]No managed pins to audit for {report.ecosystem.value}.[/green]"

    headline_parts: list[str] = []
    if report.stale:
        headline_parts.append(f"[yellow]{len(report.stale)} stale[/yellow]")
    if report.yanked:
        headline_parts.append(f"[red]{len(report.yanked)} yanked[/red]")
    if report.fresh:
        headline_parts.append(f"[green]{len(report.fresh)} fresh[/green]")
    if report.unknown:
        headline_parts.append(f"[cyan]{len(report.unknown)} unknown[/cyan]")
    headline: RenderableType = (
        f"Audited [bold]{len(report.entries)}[/bold] managed pin(s) "
        f"for [bold]{report.ecosystem.value}[/bold]: " + ", ".join(headline_parts)
    )

    sections: list[RenderableType] = [headline]
    if report.stale:
        sections.append(_render_audit_bucket("Stale (pin can be retired)", AuditStatus.STALE, report.stale))
    if report.yanked:
        sections.append(_render_audit_bucket("Yanked (pin can be retired)", AuditStatus.YANKED, report.yanked))
    if report.fresh:
        sections.append(_render_audit_bucket("Fresh (still in cooldown)", AuditStatus.FRESH, report.fresh))
    if report.unknown:
        sections.append(_render_audit_bucket("Unknown (review manually)", AuditStatus.UNKNOWN, report.unknown))
    return Group(*sections)

"""
Shared dataclasses representing packages, registry data, violations, and fix actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pendulum

from chill_out.constants import ReleaseType, EcosystemKind


@dataclass(frozen=True)
class InstalledPackage:
    """A single installed dependency that should be checked against the cooldown rules."""

    name: str
    version: str
    ecosystem: EcosystemKind
    via_chain: tuple[str, ...] = ()
    """
    Reverse path from this package up to the principal dependency that pulled it in.

    Empty tuple means the package is a principal (declared directly in pyproject/package.json).
    The first element is the immediate parent, the last is the principal.
    """

    @property
    def via(self) -> str | None:
        """The principal dependency at the top of the chain, if this is a transitive dep."""
        return self.via_chain[-1] if self.via_chain else None


@dataclass(frozen=True)
class PackageRelease:
    """A single released version of a package, with its publish timestamp."""

    version: str
    published: pendulum.DateTime


@dataclass(frozen=True)
class PackageInfo:
    """All releases known for a package, keyed by version string."""

    name: str
    releases: dict[str, PackageRelease]

    def published_at(self, version: str) -> pendulum.DateTime | None:
        """Return the publish timestamp for the given version, if known."""
        rel = self.releases.get(version)
        return rel.published if rel else None


@dataclass(frozen=True)
class VersionManifest:
    """
    The dependency declarations for a single (name, version) pair.

    ``deps`` maps each declared dependency name to its raw range spec, in the
    native format of the ecosystem (e.g. ``"^2.0.0"`` for npm or
    ``">=2.5,<3.0"`` for PyPI).
    """

    name: str
    version: str
    deps: dict[str, str]


@dataclass(frozen=True)
class SafeVersion:
    """A version older than the installed one that has cleared its cooldown window."""

    version: str
    age_days: int


@dataclass
class Violation:
    """A package whose installed version has not cleared its cooldown window."""

    package: InstalledPackage
    release_type: ReleaseType
    age_days: int
    limit_days: int
    published: pendulum.DateTime
    safe_version: SafeVersion | None = None

    @property
    def name(self) -> str:
        return self.package.name

    @property
    def version(self) -> str:
        return self.package.version

    @property
    def via(self) -> str | None:
        return self.package.via


@dataclass(frozen=True)
class FixAction:
    """A single change to apply when running `--fix`.

    Both direct and transitive violations land in the same shape: a pin of
    ``package`` to ``version`` written to the project's primary manifest
    (``project.dependencies`` for pypi, ``dependencies`` for npm). Transitive
    pins ride along as direct entries; the ecosystem resolver hoists them.
    """

    package: str
    version: str


@dataclass(frozen=True)
class UnfixableViolation:
    """A violation that ``--fix`` could not auto-resolve.

    Surfaces the structured reason so the CLI can print actionable guidance
    instead of silently dropping the violation.
    """

    violation: Violation
    reason: str


@dataclass
class FixPlan:
    """The result of planning fixes for a check report."""

    actions: list[FixAction] = field(default_factory=list)
    unfixable: list[UnfixableViolation] = field(default_factory=list)


@dataclass
class CheckReport:
    """Aggregated outcome of a check run."""

    ecosystem: EcosystemKind
    checked: list[InstalledPackage]
    violations: list[Violation] = field(default_factory=list)
    skipped: list[tuple[InstalledPackage, str]] = field(default_factory=list)

    @property
    def has_violations(self) -> bool:
        return bool(self.violations)

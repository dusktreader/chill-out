"""
Protocol for ecosystem backends.
"""

from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

from chill_out.constants import EcosystemKind
from chill_out.ecosystems.version_parsing import ParsedVersion
from chill_out.models import (
    AppliedFixes,
    FixAction,
    InstalledPackage,
    PackageInfo,
    VersionManifest,
    WorkspaceTopology,
)
from chill_out.state import ManagedPin, RemovalOutcome


@runtime_checkable
class Ecosystem(Protocol):
    """
    Pluggable backend for one package ecosystem (npm, pypi, ...).

    The Protocol is structural, so a backend only has to expose the right
    methods to satisfy it; chill-out's own backends inherit explicitly so
    type checkers flag any drift at the class definition rather than at a
    call site. Each backend owns a `root` directory (the project being
    audited) and advertises its `kind`, then implements every method below.
    Project detection lives on a separate `EcosystemDetector` so the
    registry can ask "which ecosystem applies?" without having to construct
    an instance first.

    Backends also speak directly to their registry: `fetch_package` and
    `fetch_version_manifest` are async methods that take an
    `httpx.AsyncClient` per call so the caller (typically a `RegistryClient`)
    owns the session and the cache.
    """

    kind: EcosystemKind
    root: Path

    def load_installed(self) -> list[InstalledPackage]:
        """
        Enumerate every package in the project's lockfile.

        Returns the full resolved dependency set, principals and transitives
        alike. Each `InstalledPackage` carries enough context (`via_chain`,
        `groups`, `member_owners`) for downstream filters and fix planning to
        tell them apart.
        """
        ...

    async def fetch_package(self, name: str, http: httpx.AsyncClient) -> PackageInfo | None:
        """
        Return all release info for `name`, or `None` if it cannot be retrieved.

        Caching and in-flight dedupe live one layer up in `RegistryClient`;
        backends do neither and just translate one HTTP call into a
        `PackageInfo`.
        """
        ...

    async def fetch_version_manifest(self, name: str, version: str, http: httpx.AsyncClient) -> VersionManifest | None:
        """
        Return the dependency declarations for a single (name, version) pair.

        Used by principal-rollback to discover which transitive ranges a candidate
        principal version declares. Returns `None` if the manifest cannot be
        retrieved.
        """
        ...

    def apply_fixes(self, actions: list[FixAction]) -> AppliedFixes:
        """
        Apply the given fix actions to the project.

        Returns an `AppliedFixes` carrying one `AppliedFix` per entry actually
        written to the project's manifests, plus a list of human-readable log
        lines describing the changes for the CLI to surface. The per-entry
        records capture the literal `pinned_spec` written to disk so the next
        run can detect whether the user has since edited it (drift) and clean
        stale entries up before planning fresh fixes.
        """
        ...

    def range_satisfies(self, version: str, range_spec: str) -> bool:
        """
        Return True if `version` satisfies the ecosystem-specific `range_spec`.

        Used by principal-rollback to test whether a candidate principal's
        declared range admits the safe transitive version.
        """
        ...

    def parse_version(self, version: str) -> ParsedVersion | None:
        """
        Parse a version string the way this ecosystem parses one.

        Returns `None` for inputs that don't fit the ecosystem's version
        grammar; the cooldown engine treats that as "skip this candidate"
        rather than raising, so a single weird release never blocks the rest
        of the search. The returned `ParsedVersion` carries everything
        the engine needs (release segments, pre-release flag, and an opaque
        sort key) without the engine having to know which flavor of version
        it's looking at.
        """
        ...

    def supports_overrides(self) -> bool:
        """Return True if this ecosystem implements an override mechanism.

        Most package managers expose some flavor of "force one resolution
        everywhere regardless of who declared it" knob (npm `overrides`, yarn
        `resolutions`, pnpm `pnpm.overrides`, uv `override-dependencies`,
        cargo `[patch]`, go `replace`, maven `dependencyManagement`, gradle
        `resolutionStrategy.force`). A handful of others, notably bundler and
        composer, don't, so backends for those ecosystems return False here
        and the runner falls back to plain direct pins.
        """
        ...

    def apply_override_fixes(self, actions: list[FixAction]) -> AppliedFixes | None:
        """
        Apply fixes via the ecosystem's override mechanism.

        Used as a fallback when a normal direct pin doesn't dislodge a
        violating version (typically because it stays hoisted at a parent
        level the direct pin can't reach). The exact mechanism varies by
        ecosystem (see `supports_overrides` for a survey); the contract here
        is the same regardless of which one a backend reaches for.

        Returns an `AppliedFixes` carrying one entry per override actually
        written plus human-readable log lines, or `None` when the ecosystem
        doesn't support an override mechanism.
        """
        ...

    def remove_managed_pin(self, pin: ManagedPin) -> RemovalOutcome:
        """
        Try to undo a previously-applied managed pin from this project's manifests.

        Used by the fix workflow to clean stale pins before computing a new round of fixes,
        so cooldowns that have elapsed in the meantime do not leave their pins behind.

        Implementations look up `pin.package` at the site recorded in `pin.manifest_path`
        (interpreted relative to `self.root`) using the appropriate mechanism for
        `pin.mechanism`, and:

        * Return `RemovalOutcome.REMOVED` if the entry is still present and matches the
          recorded `pin.pinned_spec`. The entry is deleted in place.
        * Return `RemovalOutcome.DRIFTED` if the entry is present but its value differs
          from the recorded value. Implementations leave the entry untouched; the caller
          is expected to drop the pin from state and warn the user.
        * Return `RemovalOutcome.ORPHAN` if the entry is no longer present at all.
          Implementations leave the manifest alone; the caller drops the pin silently.

        Implementations must not run lockfile regeneration; the runner orchestrates that
        step once after the full batch of removals.
        """
        ...

    def regenerate_lockfile(self) -> str:
        """
        Recompute the project's lockfile from its current manifests.

        Used by the fix workflow after a cleanup pass that removed stale managed pins but did
        not apply any fresh fixes (the apply step regenerates the lockfile on its own when it
        runs). Returns a short human-readable line describing the action taken so the CLI can
        surface it in its log output.

        Implementations should raise `EcosystemError` if regeneration fails.
        """
        ...

    def workspace_topology(self) -> WorkspaceTopology | None:
        """
        Detect a multi-member workspace and return its layout.

        Returns `None` for standalone (single-root) projects or when no
        workspace declaration is present.
        """
        ...

"""
Ecosystem-agnostic version parsing types for the cooldown engine.

The cooldown logic in `chill_out.cooldown` needs to do four things with a version string:
classify it as major / minor / patch, compare it to other versions, recognize pre-releases,
and round-trip back to its original string form for fix planning. None of that requires the
engine to know whether the version came from semver, PEP 440, or something else, so each
ecosystem provides its own parser and the engine works through the small common interface
declared here.

`ParsedVersion` carries exactly the four pieces of information the engine needs.
`VersionParser` is the structural type the ecosystem backends' `parse_version` methods
conform to; pass `ecosystem.parse_version` wherever a `VersionParser` is expected.

The concrete implementations live on each ecosystem backend:

- `chill_out.ecosystems.backend.Ecosystem.parse_version` — the abstract contract.
- `chill_out.ecosystems.npm.backend.NpmEcosystem.parse_version` — semver via `node-semver`.
- `chill_out.ecosystems.pypi.backend.PypiEcosystem.parse_version` — PEP 440 via `packaging`.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Self


@dataclass(frozen=True)
class ParsedVersion:
    """
    Engine-friendly view of a parsed version.

    Two parsed versions compare via their `sort_key`, so each ecosystem
    decides what "newer than" means for its own version flavor (semver does
    one thing with pre-releases, PEP 440 does another with post-releases and
    epochs, and so on). The original string is preserved verbatim so safe
    versions round-trip back into manifests and lockfiles in the form the
    registry actually publishes.
    """

    original: str
    """The version string as it came from the registry. Used verbatim by
    downstream callers so we never accidentally rename a version on the way
    back out."""

    major: int
    """First numeric segment of the release. Used by `release_type`."""

    minor: int
    """Second numeric segment of the release. Used by `release_type`."""

    micro: int
    """Third numeric segment of the release. Called *patch* in semver-speak.
    Used by `release_type`."""

    is_prerelease: bool
    """True for any non-final release (alpha / beta / rc / dev). Pre-releases
    are excluded from rollback candidates because betas aren't safer than the
    version we're trying to replace."""

    sort_key: tuple[Any, ...] = field(compare=False, repr=False)
    """Opaque ordering tuple. Built by the ecosystem's parser; the engine
    only ever passes this to `<` / `max`."""

    def __lt__(self, other: Self) -> bool:
        return self.sort_key < other.sort_key

    def __le__(self, other: Self) -> bool:
        return self.sort_key <= other.sort_key

    def __gt__(self, other: Self) -> bool:
        return self.sort_key > other.sort_key

    def __ge__(self, other: Self) -> bool:
        return self.sort_key >= other.sort_key


VersionParser = Callable[[str], ParsedVersion | None]
"""
A function that parses a version string, returning `None` for inputs the
ecosystem can't make sense of. The cooldown engine treats `None` as "skip
this candidate" rather than raising, so a single weird release never blocks
the rest of the search.
"""

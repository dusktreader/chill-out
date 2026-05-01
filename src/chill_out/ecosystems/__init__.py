"""
Pluggable ecosystem backends.

Each backend knows how to:

- Enumerate installed packages (direct or transitive).
- Talk to the matching registry to get release dates.
- Apply a fix by editing manifest files and (optionally) re-running the package manager.

Detection of which backend applies to a project root lives on a separate
`EcosystemDetector` per ecosystem, so the registry can probe candidates without
having to construct a backend instance up front.

The protocol definitions live alongside this package as `detector`, `backend`,
and `fetcher`; per-ecosystem implementations live in their own subpackages
(`npm`, `pypi`). Import directly from those modules; this package only
re-exports the registry helpers callers need to look an ecosystem up by kind
or auto-detect one from a project root.
"""

from chill_out.ecosystems.registry import detect_ecosystem, get_ecosystem

__all__ = [
    "detect_ecosystem",
    "get_ecosystem",
]

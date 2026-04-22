"""
Lookup helpers that select the right ecosystem for a project root.
"""

from __future__ import annotations

from pathlib import Path

from chill_out.constants import EcosystemKind
from chill_out.ecosystems.base import Ecosystem
from chill_out.ecosystems.npm import NpmEcosystem
from chill_out.ecosystems.pypi import PypiEcosystem
from chill_out.exceptions import EcosystemError

_REGISTRY: dict[EcosystemKind, type[Ecosystem]] = {
    EcosystemKind.NPM: NpmEcosystem,
    EcosystemKind.PYPI: PypiEcosystem,
}


def get_ecosystem(kind: EcosystemKind, root: Path) -> Ecosystem:
    """Instantiate a backend by kind for the given project root."""
    cls = _REGISTRY.get(kind)
    if cls is None:
        raise EcosystemError(f"Unknown ecosystem: {kind}")
    return cls(root)


def detect_ecosystem(root: Path) -> Ecosystem:
    """
    Auto-detect which ecosystem backend applies to the given project root.

    Raises:
        EcosystemError: If no backend matches, or if multiple backends match
            (in which case the user should pass the ecosystem explicitly).
    """
    matches = [cls for cls in _REGISTRY.values() if cls.detect(root)]
    EcosystemError.require_condition(
        len(matches) > 0,
        f"Could not detect a supported ecosystem in {root}. Looked for npm (package.json) and pypi (pyproject.toml).",
    )
    EcosystemError.require_condition(
        len(matches) == 1,
        f"Multiple ecosystems detected in {root}: {[m.kind.value for m in matches]}. "
        "Pass --ecosystem explicitly to disambiguate.",
    )
    return matches[0](root)

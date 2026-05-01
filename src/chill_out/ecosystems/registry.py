"""
Lookup helpers that select the right ecosystem for a project root.
"""

from pathlib import Path

from chill_out.constants import EcosystemKind
from chill_out.ecosystems.backend import Ecosystem
from chill_out.ecosystems.detector import EcosystemDetector
from chill_out.ecosystems.npm.backend import NpmEcosystem
from chill_out.ecosystems.npm.detector import NpmDetector
from chill_out.ecosystems.pypi.backend import PypiEcosystem
from chill_out.ecosystems.pypi.detector import PypiDetector
from chill_out.exceptions import EcosystemError

_REGISTRY: dict[EcosystemKind, tuple[EcosystemDetector, type[Ecosystem]]] = {
    EcosystemKind.NPM: (NpmDetector(), NpmEcosystem),
    EcosystemKind.PYPI: (PypiDetector(), PypiEcosystem),
}


def get_ecosystem(kind: EcosystemKind, root: Path) -> Ecosystem:
    """Instantiate a backend by kind for the given project root."""
    entry = _REGISTRY.get(kind)
    if entry is None:  # pragma: no cover - defensive guard for unregistered ecosystem kinds
        raise EcosystemError(f"Unknown ecosystem: {kind}")
    _, cls = entry
    return cls(root)


def detect_ecosystem(root: Path) -> Ecosystem:
    """
    Auto-detect which ecosystem backend applies to the given project root.

    Raises:
        EcosystemError: If no backend matches, or if multiple backends match
            (in which case the user should pass the ecosystem explicitly).
    """
    matches = [(kind, cls) for kind, (detector, cls) in _REGISTRY.items() if detector.detect(root)]
    EcosystemError.require_condition(
        len(matches) > 0,
        f"Could not detect a supported ecosystem in {root}. Looked for npm (package.json) and pypi (pyproject.toml).",
    )
    EcosystemError.require_condition(
        len(matches) == 1,
        f"Multiple ecosystems detected in {root}: {[kind.value for kind, _ in matches]}. "
        "Pass --ecosystem explicitly to disambiguate.",
    )
    return matches[0][1](root)

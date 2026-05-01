"""
Protocol for ecosystem project detectors.
"""

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class EcosystemDetector(Protocol):
    """
    Probe that reports whether an ecosystem applies to a given project root.

    Detectors are stateless: instantiated once and reused. The registry walks
    its detectors in order and asks each one whether the project at `root`
    looks like its ecosystem (npm sees a `package.json`, pypi sees a
    `pyproject.toml`, and so on). The matching detector's paired ecosystem
    class is then constructed for that root.

    Keeping detection on its own object decouples "should we use this
    ecosystem?" from "how do we drive it?", which keeps the `Ecosystem`
    protocol focused purely on instance-level work.

    The Protocol is structural, so a detector only has to expose `detect` to
    satisfy it; chill-out's own detectors inherit explicitly so type checkers
    flag any drift at the class definition rather than at a call site.
    """

    def detect(self, root: Path) -> bool:
        """Return True if this ecosystem applies to the given project root."""
        ...

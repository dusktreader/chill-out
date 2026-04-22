"""
Pluggable ecosystem backends.

Each backend knows how to:

- Detect whether it applies to a given project root (via :meth:`Ecosystem.detect`).
- Enumerate installed packages (direct or transitive).
- Talk to the matching registry to get release dates.
- Apply a fix by editing manifest files and (optionally) re-running the package manager.
"""

from chill_out.ecosystems.base import Ecosystem, RegistryClient
from chill_out.ecosystems.npm import NpmEcosystem, NpmRegistryClient
from chill_out.ecosystems.pypi import PypiEcosystem, PypiRegistryClient
from chill_out.ecosystems.registry import detect_ecosystem, get_ecosystem

__all__ = [
    "Ecosystem",
    "RegistryClient",
    "NpmEcosystem",
    "NpmRegistryClient",
    "PypiEcosystem",
    "PypiRegistryClient",
    "detect_ecosystem",
    "get_ecosystem",
]

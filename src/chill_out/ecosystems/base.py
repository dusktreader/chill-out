"""
Abstract base classes for ecosystem backends and registry clients.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import httpx

from chill_out.constants import EcosystemKind
from chill_out.models import FixAction, InstalledPackage, PackageInfo


class RegistryClient(ABC):
    """
    Async client that fetches package information from a registry.

    Implementations are expected to be safe for concurrent use behind a single
    underlying ``httpx.AsyncClient``.
    """

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    @abstractmethod
    async def fetch_package(self, name: str) -> PackageInfo | None:
        """Return all release info for a package, or ``None`` if it cannot be retrieved."""
        ...


class Ecosystem(ABC):
    """
    Pluggable backend for one package ecosystem (npm, pypi, ...).
    """

    kind: EcosystemKind

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    @abstractmethod
    def detect(cls, root: Path) -> bool:
        """Return True if this ecosystem applies to the given project root."""
        ...

    @abstractmethod
    def load_installed(self, *, deep: bool = False) -> list[InstalledPackage]:
        """
        Enumerate installed packages.

        Args:
            deep: When True, include transitive dependencies; otherwise only
                packages declared directly by the project.
        """
        ...

    @abstractmethod
    def make_client(self, http: httpx.AsyncClient) -> RegistryClient:
        """Construct a registry client bound to the given HTTP session."""
        ...

    @abstractmethod
    def apply_fixes(self, actions: list[FixAction]) -> list[str]:
        """
        Apply the given fix actions to the project.

        Returns:
            A list of human-readable lines describing the changes that were made.
        """
        ...

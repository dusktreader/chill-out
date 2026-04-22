"""Unit tests for ecosystem detection and base classes."""

from __future__ import annotations

from pathlib import Path

import pytest
from chill_out.constants import EcosystemKind
from chill_out.ecosystems import NpmEcosystem, PypiEcosystem, detect_ecosystem, get_ecosystem
from chill_out.exceptions import EcosystemError


class TestDetect:
    def test_npm_detected_by_package_json(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        eco = detect_ecosystem(tmp_path)
        assert eco.kind is EcosystemKind.NPM
        assert isinstance(eco, NpmEcosystem)

    def test_pypi_detected_by_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n")
        eco = detect_ecosystem(tmp_path)
        assert eco.kind is EcosystemKind.PYPI
        assert isinstance(eco, PypiEcosystem)

    def test_no_match_raises(self, tmp_path: Path) -> None:
        with pytest.raises(EcosystemError, match="Could not detect"):
            detect_ecosystem(tmp_path)

    def test_multiple_matches_raises(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n")
        with pytest.raises(EcosystemError, match="Multiple ecosystems"):
            detect_ecosystem(tmp_path)


class TestGetEcosystem:
    def test_returns_npm(self, tmp_path: Path) -> None:
        eco = get_ecosystem(EcosystemKind.NPM, tmp_path)
        assert isinstance(eco, NpmEcosystem)

    def test_returns_pypi(self, tmp_path: Path) -> None:
        eco = get_ecosystem(EcosystemKind.PYPI, tmp_path)
        assert isinstance(eco, PypiEcosystem)

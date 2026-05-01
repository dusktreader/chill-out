"""Unit tests for the version-resolution helpers."""

from importlib import metadata
from pathlib import Path
from unittest.mock import patch

from chill_out import version as version_module


class TestGetVersionFromPyproject:
    def test_reads_version_field_from_local_pyproject(self, tmp_path: Path, monkeypatch) -> None:
        """`get_version_from_pyproject` opens `pyproject.toml` in cwd and returns `[project].version`."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "9.9.9"\n')
        monkeypatch.chdir(tmp_path)
        assert version_module.get_version_from_pyproject() == "9.9.9"


class TestGetVersionFallbacks:
    def test_falls_back_to_pyproject_when_metadata_missing(self, tmp_path: Path, monkeypatch) -> None:
        """When the package isn't installed, `get_version` reads `pyproject.toml` instead."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "7.7.7"\n')
        monkeypatch.chdir(tmp_path)
        with patch.object(version_module, "get_version_from_metadata", side_effect=metadata.PackageNotFoundError):
            assert version_module.get_version() == "7.7.7"

    def test_returns_unknown_when_both_paths_fail(self, tmp_path: Path, monkeypatch) -> None:
        """When metadata is missing and pyproject can't be read, `get_version` returns `"unknown"`."""
        monkeypatch.chdir(tmp_path)  # No pyproject.toml present.
        with patch.object(version_module, "get_version_from_metadata", side_effect=metadata.PackageNotFoundError):
            assert version_module.get_version() == "unknown"

"""Unit tests for PypiDetector."""

from pathlib import Path

from chill_out.ecosystems.pypi.detector import PypiDetector


class TestPypiDetector:
    def test_true_with_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n")
        assert PypiDetector().detect(tmp_path) is True

    def test_false_without(self, tmp_path: Path) -> None:
        assert PypiDetector().detect(tmp_path) is False

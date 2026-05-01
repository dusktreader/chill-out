"""Unit tests for NpmDetector."""

import json
from pathlib import Path

from chill_out.ecosystems.npm.detector import NpmDetector


def _write_pkg_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


class TestNpmDetector:
    def test_true_with_package_json(self, tmp_path: Path) -> None:
        _write_pkg_json(tmp_path / "package.json", {"name": "x"})
        assert NpmDetector().detect(tmp_path) is True

    def test_false_without_package_json(self, tmp_path: Path) -> None:
        assert NpmDetector().detect(tmp_path) is False

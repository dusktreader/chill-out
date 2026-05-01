"""Project detector for pypi."""

from pathlib import Path

from chill_out.ecosystems.detector import EcosystemDetector


class PypiDetector(EcosystemDetector):
    """Detector for pypi projects: a `pyproject.toml` at the project root marks one."""

    def detect(self, root: Path) -> bool:
        return (root / "pyproject.toml").is_file()

"""Project detector for npm."""

from pathlib import Path

from chill_out.ecosystems.detector import EcosystemDetector


class NpmDetector(EcosystemDetector):
    """Detector for npm projects: a `package.json` at the project root marks one."""

    def detect(self, root: Path) -> bool:
        return (root / "package.json").is_file()

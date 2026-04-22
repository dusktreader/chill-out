"""
Constants and enums shared across `chill-out`.
"""

from enum import IntEnum, StrEnum


class ExitCode(IntEnum):
    """Exit codes returned by the CLI."""

    SUCCESS = 0
    GENERAL_ERROR = 1
    COOLDOWN_VIOLATION = 2
    CONFIG_ERROR = 3
    ECOSYSTEM_ERROR = 4
    REGISTRY_ERROR = 5
    INTERNAL_ERROR = 99


class BumpType(StrEnum):
    """Semver bump category used to look up cooldown thresholds."""

    MAJOR = "major"
    MINOR = "minor"
    PATCH = "patch"
    DEFAULT = "default"


class EcosystemKind(StrEnum):
    """Supported package ecosystems."""

    NPM = "npm"
    PYPI = "pypi"


# Default cooldown thresholds (in days) used when no config source supplies them.
DEFAULT_COOLDOWN_DAYS: dict[BumpType, int] = {
    BumpType.MAJOR: 30,
    BumpType.MINOR: 10,
    BumpType.PATCH: 7,
    BumpType.DEFAULT: 5,
}

# Conservative concurrency cap for registry HTTP requests.
DEFAULT_CONCURRENCY = 25

# HTTP request timeout (in seconds) used for registry calls.
DEFAULT_TIMEOUT = 15.0

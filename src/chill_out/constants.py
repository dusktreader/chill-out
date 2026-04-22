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


class ReleaseType(StrEnum):
    """Classification of a single release used to look up its cooldown threshold."""

    MAJOR = "major"
    MINOR = "minor"
    PATCH = "patch"
    DEFAULT = "default"


class EcosystemKind(StrEnum):
    """Supported package ecosystems."""

    NPM = "npm"
    PYPI = "pypi"


# Default cooldown thresholds (in days) used when no config source supplies them.
DEFAULT_COOLDOWN_DAYS: dict[ReleaseType, int] = {
    ReleaseType.MAJOR: 30,
    ReleaseType.MINOR: 10,
    ReleaseType.PATCH: 7,
    ReleaseType.DEFAULT: 5,
}

# Conservative concurrency cap for registry HTTP requests.
DEFAULT_CONCURRENCY = 25

# HTTP request timeout (in seconds) used for registry calls.
DEFAULT_TIMEOUT = 15.0

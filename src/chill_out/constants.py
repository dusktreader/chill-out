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


class DependencyGroup(StrEnum):
    """
    Semantic dependency group names used uniformly across ecosystems.

    Each ecosystem maps these names to its native concepts:

    * **npm**: ``main`` -> ``dependencies``; ``dev`` -> ``devDependencies``;
      ``optional`` -> ``optionalDependencies``; ``peer`` -> ``peerDependencies``.
    * **pypi**: ``main`` -> ``[project.dependencies]``;
      ``dev`` -> ``[dependency-groups.dev]`` plus
      ``[project.optional-dependencies.dev]`` when present;
      ``optional`` -> all other ``[project.optional-dependencies.*]`` extras.
      ``peer`` is unused on pypi.
    """

    MAIN = "main"
    DEV = "dev"
    OPTIONAL = "optional"
    PEER = "peer"


# Default set of groups checked when the config doesn't specify one.
# Restricted to ``main`` so that dev/test/optional dependencies don't trip
# cooldown checks unless the project explicitly opts them in.
DEFAULT_INCLUDE_GROUPS: tuple[DependencyGroup, ...] = (DependencyGroup.MAIN,)


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

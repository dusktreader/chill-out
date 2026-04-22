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


class FixStyle(StrEnum):
    """
    How ``--fix`` writes the new version constraint into the project manifest.

    * ``exact`` -- pin to the safe version exactly. PyPI gets ``pkg==X.Y.Z``;
      npm gets the bare version string ``X.Y.Z``. This is the historical
      default and the safest choice when you want the lockfile to be the
      source of truth and never drift on its own.
    * ``compatible`` -- write a range that admits patch and minor releases
      under the safe version's major. PyPI gets ``pkg>=<lower>,<(M+1).0.0``,
      preserving any pre-existing lower bound declared by the user (or
      falling back to ``>=X.Y.Z`` when none was declared). npm gets the
      caret form ``^X.Y.Z``, which has the same semantic in npm's resolver.
      Use this when you want routine updates to flow through without manual
      intervention but still want the cooldown to bite the next time a new
      major lands.

    Override-style fixes (``via_overrides=True``) always pin exactly
    regardless of this setting; the entire reason an override exists is to
    pin away from a specific version that just landed.
    """

    EXACT = "exact"
    COMPATIBLE = "compatible"


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


# Default ``--fix`` style. Exact pins preserve the historical behavior and
# keep the lockfile as the single source of truth; users who want routine
# patch/minor updates to flow through without manual intervention can flip
# this to ``FixStyle.COMPATIBLE``.
DEFAULT_FIX_STYLE: FixStyle = FixStyle.EXACT

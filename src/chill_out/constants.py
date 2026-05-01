"""
Constants and enums shared across `chill-out`.
"""

from enum import IntEnum

from auto_name_enum import AutoNameEnum, LowerCaseMixin, autodoc
from snick import unwrap


class ExitCode(IntEnum):
    """Exit codes returned by the CLI."""

    SUCCESS = 0
    GENERAL_ERROR = 1
    COOLDOWN_VIOLATION = 2
    CONFIG_ERROR = 3
    ECOSYSTEM_ERROR = 4
    REGISTRY_ERROR = 5
    STATE_ERROR = 6
    STATE_STALE = 7
    INTERNAL_ERROR = 99


class ReleaseType(AutoNameEnum, LowerCaseMixin):
    """Classification of a single release used to look up its cooldown threshold."""

    MAJOR = autodoc("A major release that bumps the leftmost non-zero version component.")
    MINOR = autodoc("A minor release that bumps the second version component.")
    PATCH = autodoc("A patch release that bumps only the rightmost version component.")
    DEFAULT = autodoc(
        unwrap(
            """
            Fallback bucket used when a release cannot be classified as major, minor, or patch
            (for example, an unparsable version string).
            """
        )
    )


class EcosystemKind(AutoNameEnum, LowerCaseMixin):
    """Supported package ecosystems."""

    NPM = autodoc("The npm ecosystem, driven by `package.json` and `package-lock.json`.")
    PYPI = autodoc("The PyPI ecosystem, driven by `pyproject.toml` and `uv.lock`.")


class FixStyle(AutoNameEnum, LowerCaseMixin):
    """How `chill-out fix` writes the new version constraint into the project manifest.

    Override-style fixes (`via_overrides=True`) always pin exactly regardless of this setting; the
    entire reason an override exists is to pin away from a specific version that just landed.
    """

    EXACT = autodoc(
        unwrap(
            """
            Pin to the safe version exactly. PyPI gets `pkg==X.Y.Z`; npm gets the bare version
            string `X.Y.Z`. This is the historical default and the safest choice when you want
            the lockfile to be the source of truth and never drift on its own.
            """
        )
    )
    COMPATIBLE = autodoc(
        unwrap(
            """
            Write a range that admits patch and minor releases under the safe version's major.
            PyPI gets `pkg>=<lower>,<(M+1).0.0`, preserving any pre-existing lower bound declared
            by the user (or falling back to `>=X.Y.Z` when none was declared). npm gets the caret
            form `^X.Y.Z`, which has the same semantic in npm's resolver. Use this when you want
            routine updates to flow through without manual intervention but still want the
            cooldown to bite the next time a new major lands.
            """
        )
    )


class DependencyGroup(AutoNameEnum, LowerCaseMixin):
    """Semantic dependency group names used uniformly across ecosystems."""

    MAIN = autodoc("Production dependencies. npm: `dependencies`. pypi: `[project.dependencies]`.")
    DEV = autodoc(
        unwrap(
            """
            Development-only dependencies. npm: `devDependencies`. pypi: `[dependency-groups.dev]`
            plus `[project.optional-dependencies.dev]` when present.
            """
        )
    )
    OPTIONAL = autodoc(
        unwrap(
            """
            Optional dependencies. npm: `optionalDependencies`. pypi: all other
            `[project.optional-dependencies.*]` extras.
            """
        )
    )
    PEER = autodoc("Peer dependencies. npm: `peerDependencies`. Unused on pypi.")


class AuditStatus(AutoNameEnum, LowerCaseMixin):
    """Outcome bucket assigned to each managed pin during a `chill-out audit` run."""

    FRESH = autodoc(
        unwrap(
            """
            The avoided release is still inside its cooldown window; the pin
            is doing exactly what it was written for.
            """
        )
    )
    STALE = autodoc(
        unwrap(
            """
            The avoided release has cleared cooldown since the pin was
            written. The pin can be retired (`chill-out fix --cleanup` will
            do it automatically on its next run).
            """
        )
    )
    YANKED = autodoc(
        unwrap(
            """
            The registry pulled the avoided release entirely (PyPI yank,
            npm unpublish). The pin is no longer needed; treat as stale
            with extra confidence.
            """
        )
    )
    UNKNOWN = autodoc(
        unwrap(
            """
            The audit could not determine the avoided release's current
            state -- the registry skipped the package, the version is no
            longer present in the registry response, or a publish date is
            missing. Surfaced so the user can decide whether to retire the
            pin manually rather than silently treating it as fresh.
            """
        )
    )


# Default set of groups checked when the config doesn't specify one.
# Restricted to `main` so that dev/test/optional dependencies don't trip
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


# Default `chill-out fix` style. Exact pins preserve the historical behavior and
# keep the lockfile as the single source of truth; users who want routine
# patch/minor updates to flow through without manual intervention can flip
# this to `FixStyle.COMPATIBLE`.
DEFAULT_FIX_STYLE: FixStyle = FixStyle.EXACT

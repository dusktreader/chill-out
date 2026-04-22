"""
chill-out — manage cooldown for package dependencies to avoid zero-day supply chain vulnerabilities.
"""

from loguru import logger

from chill_out.config import ChillOutConfig, CooldownConfig, load_config
from chill_out.constants import DependencyGroup, FixStyle, ReleaseType, EcosystemKind, ExitCode
from chill_out.cooldown import find_safe_version, is_within_cooldown, release_type
from chill_out.ecosystems import Ecosystem, NpmEcosystem, PypiEcosystem, detect_ecosystem, get_ecosystem
from chill_out.exceptions import (
    ChillOutError,
    ConfigError,
    CooldownViolation,
    EcosystemError,
    RegistryError,
)
from chill_out.models import (
    CheckReport,
    FixAction,
    FixPlan,
    InstalledPackage,
    PackageInfo,
    PackageRelease,
    SafeVersion,
    UnfixableViolation,
    VersionManifest,
    Violation,
)
from chill_out.runner import check, check_async, plan_fixes, plan_fixes_async
from chill_out.version import get_version

# Library callers can opt back in via `logger.enable("chill_out")`.
logger.disable("chill_out")

__version__ = get_version()

__all__ = [
    "__version__",
    "ReleaseType",
    "CheckReport",
    "ChillOutConfig",
    "ChillOutError",
    "ConfigError",
    "CooldownConfig",
    "CooldownViolation",
    "DependencyGroup",
    "Ecosystem",
    "EcosystemError",
    "EcosystemKind",
    "ExitCode",
    "FixAction",
    "FixPlan",
    "FixStyle",
    "InstalledPackage",
    "NpmEcosystem",
    "PackageInfo",
    "PackageRelease",
    "PypiEcosystem",
    "RegistryError",
    "SafeVersion",
    "UnfixableViolation",
    "VersionManifest",
    "Violation",
    "check",
    "check_async",
    "detect_ecosystem",
    "find_safe_version",
    "get_ecosystem",
    "get_version",
    "is_within_cooldown",
    "load_config",
    "plan_fixes",
    "plan_fixes_async",
    "release_type",
]

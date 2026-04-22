"""
chill-out — manage cooldown for package dependencies to avoid zero-day supply chain vulnerabilities.
"""

from loguru import logger

from chill_out.config import CooldownConfig, load_config
from chill_out.constants import BumpType, EcosystemKind, ExitCode
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
    InstalledPackage,
    PackageInfo,
    PackageRelease,
    SafeVersion,
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
    "BumpType",
    "CheckReport",
    "ChillOutError",
    "ConfigError",
    "CooldownConfig",
    "CooldownViolation",
    "Ecosystem",
    "EcosystemError",
    "EcosystemKind",
    "ExitCode",
    "FixAction",
    "InstalledPackage",
    "NpmEcosystem",
    "PackageInfo",
    "PackageRelease",
    "PypiEcosystem",
    "RegistryError",
    "SafeVersion",
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

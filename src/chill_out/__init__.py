"""
chill-out — manage cooldown for package dependencies to avoid zero-day supply chain vulnerabilities.
"""

from loguru import logger

from chill_out.config import ChillOutConfig, load_config
from chill_out.constants import DependencyGroup, EcosystemKind, ExitCode, FixStyle, ReleaseType
from chill_out.ecosystems import detect_ecosystem, get_ecosystem
from chill_out.ecosystems.backend import Ecosystem
from chill_out.ecosystems.detector import EcosystemDetector
from chill_out.ecosystems.npm.backend import NpmEcosystem
from chill_out.ecosystems.npm.detector import NpmDetector
from chill_out.ecosystems.pypi.backend import PypiEcosystem
from chill_out.ecosystems.pypi.detector import PypiDetector
from chill_out.exceptions import (
    ChillOutError,
    ConfigError,
    CooldownViolation,
    EcosystemError,
    RegistryError,
)
from chill_out.models import (
    AppliedFix,
    AppliedFixes,
    CheckReport,
    FixAction,
    FixPlan,
    InstalledPackage,
    PackageInfo,
    PackageRelease,
    SafeVersion,
    SkipReason,
    UnfixableViolation,
    VersionManifest,
    Violation,
)
from chill_out.registry_client import RegistryClient
from chill_out.runner import (
    CleanupReport,
    build_managed_pins,
    check,
    check_async,
    cleanup_managed_pins,
    plan_fixes,
    plan_fixes_async,
)
from chill_out.state import (
    STATE_FILENAME,
    AvoidingRelease,
    ChillOutState,
    ManagedPin,
    PinMechanism,
    RemovalOutcome,
    StateError,
    StateFileCorruptError,
    StateFileUnreadableError,
    StateSchemaVersionError,
    StateValidationError,
)
from chill_out.version import get_version

# Library callers can opt back in via `logger.enable("chill_out")`.
logger.disable("chill_out")

__version__ = get_version()

__all__ = [
    "__version__",
    "STATE_FILENAME",
    "AppliedFix",
    "AppliedFixes",
    "AvoidingRelease",
    "CheckReport",
    "ChillOutConfig",
    "ChillOutError",
    "ChillOutState",
    "CleanupReport",
    "ConfigError",
    "CooldownViolation",
    "DependencyGroup",
    "Ecosystem",
    "EcosystemDetector",
    "EcosystemError",
    "EcosystemKind",
    "ExitCode",
    "FixAction",
    "FixPlan",
    "FixStyle",
    "InstalledPackage",
    "ManagedPin",
    "NpmDetector",
    "NpmEcosystem",
    "PackageInfo",
    "PackageRelease",
    "PinMechanism",
    "PypiDetector",
    "PypiEcosystem",
    "RegistryClient",
    "RegistryError",
    "ReleaseType",
    "RemovalOutcome",
    "SafeVersion",
    "SkipReason",
    "StateError",
    "StateFileCorruptError",
    "StateFileUnreadableError",
    "StateSchemaVersionError",
    "StateValidationError",
    "UnfixableViolation",
    "VersionManifest",
    "Violation",
    "build_managed_pins",
    "check",
    "check_async",
    "cleanup_managed_pins",
    "detect_ecosystem",
    "get_ecosystem",
    "get_version",
    "load_config",
    "plan_fixes",
    "plan_fixes_async",
]

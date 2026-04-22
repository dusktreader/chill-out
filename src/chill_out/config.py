"""
Layered configuration loading for `chill-out`.

Sources are consulted in priority order, highest first:

1. A dedicated config file (``.chill-out.yaml`` or ``.chill-out.yml``) at the project root.
2. A ``[tool.chill-out]`` table inside ``pyproject.toml``.
3. The Dependabot ``cooldown:`` block for the matching ecosystem in ``.github/dependabot.yml``.
4. Hard-coded defaults from ``chill_out.constants.DEFAULT_COOLDOWN_DAYS``.

Each source can supply a partial mapping; missing keys cascade down through the
remaining sources.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit
import yaml

from chill_out.constants import DEFAULT_COOLDOWN_DAYS, BumpType, EcosystemKind
from chill_out.exceptions import ConfigError


@dataclass(frozen=True)
class CooldownConfig:
    """
    Resolved cooldown thresholds, in days, keyed by bump type.

    `default_days` is used whenever a release's bump type is unknown or absent
    from the explicit map.
    """

    days: dict[BumpType, int] = field(default_factory=lambda: dict(DEFAULT_COOLDOWN_DAYS))

    def for_bump(self, bump: BumpType) -> int:
        """Return the threshold (days) for the given bump type, falling back to default."""
        return self.days.get(bump, self.days.get(BumpType.DEFAULT, DEFAULT_COOLDOWN_DAYS[BumpType.DEFAULT]))


# ---------------------------------------------------------------------------
# Source loaders — each returns a partial mapping or empty dict if not present.
# ---------------------------------------------------------------------------


def _coerce_days(raw: dict[str, Any]) -> dict[BumpType, int]:
    """Map a flexible day-config dict into a typed `BumpType -> int` map."""
    aliases = {
        "major": BumpType.MAJOR,
        "minor": BumpType.MINOR,
        "patch": BumpType.PATCH,
        "default": BumpType.DEFAULT,
        "semver-major-days": BumpType.MAJOR,
        "semver-minor-days": BumpType.MINOR,
        "semver-patch-days": BumpType.PATCH,
        "default-days": BumpType.DEFAULT,
    }
    out: dict[BumpType, int] = {}
    for key, value in raw.items():
        bump = aliases.get(str(key).lower())
        if bump is None:
            continue
        try:
            out[bump] = int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Cooldown value for '{key}' must be an integer, got {value!r}") from exc
    return out


def load_chill_out_yaml(root: Path) -> dict[BumpType, int]:
    """Load thresholds from a top-level ``.chill-out.yaml`` (or ``.yml``) file."""
    for name in (".chill-out.yaml", ".chill-out.yml"):
        path = root / name
        if not path.is_file():
            continue
        try:
            doc = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Failed to parse {path}: {exc}") from exc
        cooldown = doc.get("cooldown") if isinstance(doc, dict) else None
        if not isinstance(cooldown, dict):
            return {}
        return _coerce_days(cooldown)
    return {}


def load_pyproject_table(root: Path) -> dict[BumpType, int]:
    """Load thresholds from a ``[tool.chill-out.cooldown]`` table in pyproject.toml."""
    path = root / "pyproject.toml"
    if not path.is_file():
        return {}
    try:
        doc = tomlkit.parse(path.read_text())
    except Exception as exc:  # noqa: BLE001 — tomlkit raises a variety of errors
        raise ConfigError(f"Failed to parse {path}: {exc}") from exc
    tool = doc.get("tool", {})
    co = tool.get("chill-out", {}) if isinstance(tool, dict) else {}
    cooldown = co.get("cooldown", {}) if isinstance(co, dict) else {}
    if not isinstance(cooldown, dict):
        return {}
    return _coerce_days(dict(cooldown))


def load_dependabot(root: Path, ecosystem: EcosystemKind) -> dict[BumpType, int]:
    """Load thresholds from ``.github/dependabot.yml`` for the matching ecosystem."""
    path = root / ".github" / "dependabot.yml"
    if not path.is_file():
        return {}
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Failed to parse {path}: {exc}") from exc

    target = "npm" if ecosystem is EcosystemKind.NPM else "pip"
    for entry in doc.get("updates", []) if isinstance(doc, dict) else []:
        if not isinstance(entry, dict):
            continue
        if entry.get("package-ecosystem") == target:
            cooldown = entry.get("cooldown", {})
            if isinstance(cooldown, dict):
                return _coerce_days(cooldown)
    return {}


def load_config(root: Path, ecosystem: EcosystemKind) -> CooldownConfig:
    """
    Resolve the effective cooldown configuration for the given project root and ecosystem.

    Layers are merged so higher-priority sources override lower ones key-by-key.
    """
    layers = [
        dict(DEFAULT_COOLDOWN_DAYS),
        load_dependabot(root, ecosystem),
        load_pyproject_table(root),
        load_chill_out_yaml(root),
    ]
    merged: dict[BumpType, int] = {}
    for layer in layers:
        merged.update(layer)
    return CooldownConfig(days=merged)

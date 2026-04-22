"""
Layered configuration loading for `chill-out`.

Sources are consulted in priority order, highest first:

1. A dedicated config file (``.chill-out.yaml`` or ``.chill-out.yml``) at the project root.
2. The project's primary manifest:
   - ``[tool.chill-out]`` in ``pyproject.toml`` (Python projects), or
   - the top-level ``"chill-out"`` key in ``package.json`` (npm projects).
3. The Dependabot ``cooldown:`` block for the matching ecosystem in ``.github/dependabot.yml``.
4. Hard-coded defaults from ``chill_out.constants.DEFAULT_COOLDOWN_DAYS``.

Each source can supply a partial mapping; missing keys cascade down through the
remaining sources.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit
import yaml

from chill_out.constants import DEFAULT_COOLDOWN_DAYS, ReleaseType, EcosystemKind
from chill_out.exceptions import ConfigError


@dataclass(frozen=True)
class CooldownConfig:
    """
    Resolved cooldown thresholds, in days, keyed by release type.

    `default_days` is used whenever a release's release type is unknown or absent
    from the explicit map.
    """

    days: dict[ReleaseType, int] = field(default_factory=lambda: dict(DEFAULT_COOLDOWN_DAYS))

    def for_release_type(self, rel_type: ReleaseType) -> int:
        """Return the threshold (days) for the given release type, falling back to default."""
        return self.days.get(rel_type, self.days.get(ReleaseType.DEFAULT, DEFAULT_COOLDOWN_DAYS[ReleaseType.DEFAULT]))


# ---------------------------------------------------------------------------
# Source loaders — each returns a partial mapping or empty dict if not present.
# ---------------------------------------------------------------------------


def _coerce_days(raw: dict[str, Any]) -> dict[ReleaseType, int]:
    """Map a flexible day-config dict into a typed `ReleaseType -> int` map."""
    aliases = {
        "major": ReleaseType.MAJOR,
        "minor": ReleaseType.MINOR,
        "patch": ReleaseType.PATCH,
        "default": ReleaseType.DEFAULT,
        "semver-major-days": ReleaseType.MAJOR,
        "semver-minor-days": ReleaseType.MINOR,
        "semver-patch-days": ReleaseType.PATCH,
        "default-days": ReleaseType.DEFAULT,
    }
    out: dict[ReleaseType, int] = {}
    for key, value in raw.items():
        rel_type = aliases.get(str(key).lower())
        if rel_type is None:
            continue
        try:
            out[rel_type] = int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Cooldown value for '{key}' must be an integer, got {value!r}") from exc
    return out


def load_chill_out_yaml(root: Path) -> dict[ReleaseType, int]:
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


def load_pyproject_table(root: Path) -> dict[ReleaseType, int]:
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


def load_package_json(root: Path) -> dict[ReleaseType, int]:
    """
    Load thresholds from a top-level ``"chill-out"`` key in ``package.json``.

    The key may either contain a flat day map, or wrap it in a ``"cooldown"``
    sub-key to mirror the pyproject and yaml shapes:

    .. code-block:: json

        {
          "chill-out": {
            "cooldown": {"major": 30, "minor": 14, "patch": 7, "default": 7}
          }
        }
    """
    path = root / "package.json"
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Failed to parse {path}: {exc}") from exc
    block = doc.get("chill-out") if isinstance(doc, dict) else None
    if not isinstance(block, dict):
        return {}
    cooldown = block.get("cooldown", block)
    if not isinstance(cooldown, dict):
        return {}
    return _coerce_days(cooldown)


def load_dependabot(root: Path, ecosystem: EcosystemKind) -> dict[ReleaseType, int]:
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
        load_package_json(root),
        load_chill_out_yaml(root),
    ]
    merged: dict[ReleaseType, int] = {}
    for layer in layers:
        merged.update(layer)
    return CooldownConfig(days=merged)

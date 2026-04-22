"""
Layered configuration loading for `chill-out`.

Sources are consulted in priority order, highest first:

1. A dedicated config file (``.chill-out.yaml`` or ``.chill-out.yml``) at the project root.
2. The project's primary manifest:
   - ``[tool.chill-out]`` in ``pyproject.toml`` (Python projects), or
   - the top-level ``"chill-out"`` key in ``package.json`` (npm projects).
3. The Dependabot ``cooldown:`` block for the matching ecosystem in ``.github/dependabot.yml``
   (cooldown thresholds only; Dependabot has no concept of dependency-group filtering).
4. Hard-coded defaults from ``chill_out.constants``.

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

from chill_out.constants import (
    DEFAULT_COOLDOWN_DAYS,
    DEFAULT_INCLUDE_GROUPS,
    DependencyGroup,
    EcosystemKind,
    ReleaseType,
)
from chill_out.exceptions import ConfigError


@dataclass(frozen=True)
class ChillOutConfig:
    """
    Resolved chill-out configuration.

    Holds two independent settings:

    * ``cooldown_days`` -- per release-type cooldown thresholds in days.
      ``ReleaseType.DEFAULT`` is used whenever a release's release type is
      unknown or absent from the explicit map.
    * ``include_groups`` -- the semantic dependency groups that should be
      checked. Packages whose ``InstalledPackage.groups`` doesn't intersect
      with this set are filtered out by the runner before any cooldown
      lookups happen. The default is restricted to ``main`` so dev and
      optional dependencies don't trip cooldown checks unless the project
      opts them in explicitly.
    """

    cooldown_days: dict[ReleaseType, int] = field(default_factory=lambda: dict(DEFAULT_COOLDOWN_DAYS))
    include_groups: tuple[DependencyGroup, ...] = DEFAULT_INCLUDE_GROUPS

    def for_release_type(self, rel_type: ReleaseType) -> int:
        """Return the threshold (days) for the given release type, falling back to default."""
        return self.cooldown_days.get(
            rel_type, self.cooldown_days.get(ReleaseType.DEFAULT, DEFAULT_COOLDOWN_DAYS[ReleaseType.DEFAULT])
        )

    @property
    def include_group_set(self) -> frozenset[DependencyGroup]:
        """The configured ``include_groups`` as a set, for fast membership checks."""
        return frozenset(self.include_groups)


# Backwards-compatible alias. The pre-``ChillOutConfig`` name was
# ``CooldownConfig``; keep the symbol around so external callers don't
# break, but new code should use ``ChillOutConfig``.
CooldownConfig = ChillOutConfig


# ---------------------------------------------------------------------------
# Source loaders -- each returns a partial mapping or empty dict if not present.
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


def _coerce_groups(raw: Any, *, source: str) -> tuple[DependencyGroup, ...] | None:
    """
    Map a list of group names into a typed tuple of :class:`DependencyGroup`.

    Returns ``None`` when the value is missing so the caller can distinguish
    "not configured" from "explicitly empty". An explicit empty list is
    accepted and means "check nothing"; the runner will produce an empty
    report in that case.
    """
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        raise ConfigError(f"include_groups in {source} must be a list, got {type(raw).__name__}")
    valid = {g.value for g in DependencyGroup}
    out: list[DependencyGroup] = []
    seen: set[DependencyGroup] = set()
    for entry in raw:
        name = str(entry).lower()
        if name not in valid:
            raise ConfigError(
                f"Unknown dependency group {entry!r} in {source}; valid names are {sorted(valid)}"
            )
        group = DependencyGroup(name)
        if group not in seen:
            out.append(group)
            seen.add(group)
    return tuple(out)


@dataclass
class _Layer:
    """Partial config from a single source."""

    cooldown_days: dict[ReleaseType, int] = field(default_factory=dict)
    include_groups: tuple[DependencyGroup, ...] | None = None


def load_chill_out_yaml(root: Path) -> _Layer:
    """Load thresholds and group filters from a top-level ``.chill-out.yaml`` (or ``.yml``) file."""
    for name in (".chill-out.yaml", ".chill-out.yml"):
        path = root / name
        if not path.is_file():
            continue
        try:
            doc = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Failed to parse {path}: {exc}") from exc
        if not isinstance(doc, dict):
            return _Layer()
        cooldown = doc.get("cooldown")
        days = _coerce_days(cooldown) if isinstance(cooldown, dict) else {}
        groups = _coerce_groups(doc.get("include_groups"), source=str(path))
        return _Layer(cooldown_days=days, include_groups=groups)
    return _Layer()


def load_pyproject_table(root: Path) -> _Layer:
    """Load thresholds and group filters from ``[tool.chill-out]`` in pyproject.toml."""
    path = root / "pyproject.toml"
    if not path.is_file():
        return _Layer()
    try:
        doc = tomlkit.parse(path.read_text())
    except Exception as exc:  # noqa: BLE001 -- tomlkit raises a variety of errors
        raise ConfigError(f"Failed to parse {path}: {exc}") from exc
    tool = doc.get("tool", {})
    co = tool.get("chill-out", {}) if isinstance(tool, dict) else {}
    if not isinstance(co, dict):
        return _Layer()
    cooldown = co.get("cooldown", {})
    days = _coerce_days(dict(cooldown)) if isinstance(cooldown, dict) else {}
    groups = _coerce_groups(co.get("include_groups"), source=str(path))
    return _Layer(cooldown_days=days, include_groups=groups)


def load_package_json(root: Path) -> _Layer:
    """
    Load thresholds and group filters from a top-level ``"chill-out"`` key in ``package.json``.

    The key may either contain a flat day map (legacy shape), or a fully
    nested object with ``"cooldown"`` and ``"include_groups"`` sub-keys to
    mirror the pyproject and yaml shapes:

    .. code-block:: json

        {
          "chill-out": {
            "cooldown": {"major": 30, "minor": 14, "patch": 7, "default": 7},
            "include_groups": ["main", "dev"]
          }
        }
    """
    path = root / "package.json"
    if not path.is_file():
        return _Layer()
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Failed to parse {path}: {exc}") from exc
    block = doc.get("chill-out") if isinstance(doc, dict) else None
    if not isinstance(block, dict):
        return _Layer()
    cooldown = block.get("cooldown", block)
    days = _coerce_days(cooldown) if isinstance(cooldown, dict) else {}
    groups = _coerce_groups(block.get("include_groups"), source=str(path))
    return _Layer(cooldown_days=days, include_groups=groups)


def load_dependabot(root: Path, ecosystem: EcosystemKind) -> _Layer:
    """Load thresholds from ``.github/dependabot.yml`` for the matching ecosystem.

    Dependabot has no concept of dependency-group filtering, so this loader
    only ever supplies cooldown thresholds.
    """
    path = root / ".github" / "dependabot.yml"
    if not path.is_file():
        return _Layer()
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
                return _Layer(cooldown_days=_coerce_days(cooldown))
    return _Layer()


def load_config(root: Path, ecosystem: EcosystemKind) -> ChillOutConfig:
    """
    Resolve the effective chill-out configuration for the given project root and ecosystem.

    Layers are merged so higher-priority sources override lower ones.
    Cooldown thresholds merge key-by-key; ``include_groups`` is taken
    wholesale from the highest-priority source that supplies it.
    """
    layers = [
        load_dependabot(root, ecosystem),
        load_pyproject_table(root),
        load_package_json(root),
        load_chill_out_yaml(root),
    ]

    merged_days: dict[ReleaseType, int] = dict(DEFAULT_COOLDOWN_DAYS)
    merged_groups: tuple[DependencyGroup, ...] = DEFAULT_INCLUDE_GROUPS
    for layer in layers:
        merged_days.update(layer.cooldown_days)
        if layer.include_groups is not None:
            merged_groups = layer.include_groups
    return ChillOutConfig(cooldown_days=merged_days, include_groups=merged_groups)

"""
Layered configuration loading for `chill-out`.

Sources are consulted in priority order, highest first:

1. A dedicated config file at the project root, in any of `.chill-out.yaml`,
   `.chill-out.yml`, `.chill-out.toml`, or `.chill-out.json`. Only one such
   file is permitted; having more than one is a configuration error.
2. The project's primary manifest:
   - `[tool.chill-out]` in `pyproject.toml` (Python projects), or
   - the top-level `"chill-out"` key in `package.json` (npm projects).
3. The Dependabot `cooldown:` block for the matching ecosystem in `.github/dependabot.yml`
   (cooldown thresholds only; Dependabot has no concept of dependency-group filtering).
4. Hard-coded defaults from `chill_out.constants`.

Each source can supply a partial mapping; missing keys cascade down through the
remaining sources.
"""

import json
from pathlib import Path
from typing import Any

import tomlkit
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from snick import unwrap

from chill_out.constants import (
    DEFAULT_COOLDOWN_DAYS,
    DEFAULT_FIX_STYLE,
    DEFAULT_INCLUDE_GROUPS,
    DependencyGroup,
    EcosystemKind,
    FixStyle,
    ReleaseType,
)
from chill_out.exceptions import ConfigError


class ChillOutConfig(BaseModel):
    """
    Resolved chill-out configuration.

    Built from a single config source by `load_config`. Fields the source
    didn't specify are filled from module-level defaults; for `cooldown_days`
    specifically, missing release types are filled per-key from
    `DEFAULT_COOLDOWN_DAYS`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cooldown_days: dict[ReleaseType, int] = Field(
        default_factory=lambda: dict(DEFAULT_COOLDOWN_DAYS),
        description=unwrap(
            """
            Per release-type cooldown thresholds in days. `ReleaseType.DEFAULT` is used whenever
            a release's release type is unknown or absent from the explicit map.
            """
        ),
    )
    include_groups: tuple[DependencyGroup, ...] = Field(
        default=DEFAULT_INCLUDE_GROUPS,
        description=unwrap(
            """
            The semantic dependency groups that should be checked. Packages whose
            `InstalledPackage.groups` doesn't intersect with this set are filtered out by the
            runner before any cooldown lookups happen. The default is restricted to `main` so
            dev and optional dependencies don't trip cooldown checks unless the project opts
            them in explicitly.
            """
        ),
    )
    fix_style: FixStyle = Field(
        default=DEFAULT_FIX_STYLE,
        description=unwrap(
            """
            How `chill-out fix` writes the new version constraint. See
            `chill_out.constants.FixStyle` for the available styles.
            """
        ),
    )

    @field_validator("cooldown_days", mode="before")
    @classmethod
    def _validate_cooldown_days(cls, raw: Any) -> dict[ReleaseType, int]:
        """
        Coerce the input and fill any release types not provided from `DEFAULT_COOLDOWN_DAYS`.

        Whatever the source supplies wins for the keys it sets; the rest fall
        back to the built-in defaults so callers don't have to repeat the full
        table just to override one tier.
        """
        merged: dict[ReleaseType, int] = dict(DEFAULT_COOLDOWN_DAYS)
        merged.update(cls.coerce_days(raw))
        return merged

    @field_validator("include_groups", mode="before")
    @classmethod
    def _validate_include_groups(cls, raw: Any) -> tuple[DependencyGroup, ...]:
        coerced = cls.coerce_groups(raw, source="<input>")
        if coerced is None:
            return DEFAULT_INCLUDE_GROUPS
        return coerced

    @field_validator("fix_style", mode="before")
    @classmethod
    def _validate_fix_style(cls, raw: Any) -> FixStyle:
        coerced = cls.coerce_fix_style(raw, source="<input>")
        if coerced is None:
            return DEFAULT_FIX_STYLE
        return coerced

    @classmethod
    def coerce_days(cls, raw: Any) -> dict[ReleaseType, int]:
        """
        Map a day-config dict into a typed `ReleaseType -> int` map.

        Keys must match a `ReleaseType` value (`major`, `minor`, `patch`, `default`,
        case-insensitive). Unknown keys raise `ConfigError` so user typos surface
        immediately instead of being silently ignored. Non-integer values also raise
        `ConfigError`.
        """
        if raw is None:
            return {}
        cooldown = ConfigError.ensure_type(raw, dict, message=f"cooldown must be a mapping, got {type(raw).__name__}")
        valid = sorted(r.value for r in ReleaseType)
        out: dict[ReleaseType, int] = {}
        for key, value in cooldown.items():
            with ConfigError.handle_errors(
                f"Unknown cooldown key {key!r}; valid keys are {valid}",
                handle_exc_class=ValueError,
            ):
                rel_type = ReleaseType(str(key).lower())

            with ConfigError.handle_errors(
                f"Cooldown value for '{key}' must be an integer, got {value!r}",
                handle_exc_class=(TypeError, ValueError),
            ):
                out[rel_type] = int(value)
        return out

    @classmethod
    def coerce_groups(cls, raw: Any, *, source: str) -> tuple[DependencyGroup, ...] | None:
        """
        Map a list of group names into a typed tuple of `DependencyGroup`.

        Returns `None` when the value is missing so callers can distinguish
        "not configured" from "explicitly empty". An explicit empty list is
        accepted and means "check nothing"; the runner will produce an empty
        report in that case. Unknown group names raise `ConfigError`.
        """
        if raw is None:
            return None

        ConfigError.require_condition(
            isinstance(raw, (list, tuple)),
            f"include_groups in {source} must be a list, got {type(raw).__name__}",
        )

        valid = {g.value for g in DependencyGroup}
        out: list[DependencyGroup] = []
        seen: set[DependencyGroup] = set()
        for entry in raw:
            name = str(entry).lower()
            ConfigError.require_condition(
                name in valid,
                f"Unknown dependency group {entry!r} in {source}; valid names are {sorted(valid)}",
            )

            group = DependencyGroup(name)
            if group not in seen:
                out.append(group)
                seen.add(group)

        return tuple(out)

    @classmethod
    def coerce_fix_style(cls, raw: Any, *, source: str) -> FixStyle | None:
        """
        Map a raw fix-style value into a typed `FixStyle`.

        Returns `None` when the value is missing so callers can distinguish
        "not configured" from "explicitly set". Unknown names raise
        `ConfigError` with the list of valid choices.
        """
        if raw is None:
            return None
        name = str(raw).lower()
        valid = {s.value for s in FixStyle}
        ConfigError.require_condition(
            name in valid,
            f"Unknown fix_style {raw!r} in {source}; valid choices are {sorted(valid)}",
        )
        return FixStyle(name)

    def for_release_type(self, rel_type: ReleaseType) -> int:
        """Return the cooldown threshold (days) for the given release type.

        The `cooldown_days` map is always fully populated thanks to per-key gap-fill in the field
        validator, so a direct lookup is safe for every `ReleaseType` member.
        """
        return self.cooldown_days[rel_type]

    @property
    def include_group_set(self) -> frozenset[DependencyGroup]:
        """The configured `include_groups` as a set, for fast membership checks."""
        return frozenset(self.include_groups)


# ---------------------------------------------------------------------------
# Source loaders -- each returns a fully-built `ChillOutConfig` if its source
# is present, or `None` if not. `load_config` picks exactly one and falls back
# to a default `ChillOutConfig()` if no source is found.
# ---------------------------------------------------------------------------


_CHILL_OUT_FILE_SUFFIXES: tuple[str, ...] = (".yaml", ".yml", ".toml", ".json")


def _parse_chill_out_file(path: Path) -> dict[str, Any]:
    """Parse a dedicated chill-out config file based on its suffix."""
    text = path.read_text()
    suffix = path.suffix.lower()
    try:
        if suffix in (".yaml", ".yml"):
            doc = yaml.safe_load(text) or {}
        elif suffix == ".toml":
            doc = tomlkit.parse(text)
        elif suffix == ".json":
            doc = json.loads(text) if text.strip() else {}
        else:  # pragma: no cover - callers route by `_CHILL_OUT_FILE_SUFFIXES`
            raise ConfigError(f"Unsupported config file suffix: {path}")
    except ConfigError:  # pragma: no cover - re-raise guard so the broader except below stays tidy
        raise
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Failed to parse {path}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 -- tomlkit raises a variety of errors
        raise ConfigError(f"Failed to parse {path}: {exc}") from exc
    return ConfigError.ensure_type(
        doc,
        dict,
        message=f"Top-level content of {path} must be a mapping, got {type(doc).__name__}",
    )


def _config_from_block(block: dict[str, Any], *, source: str) -> ChillOutConfig:
    """
    Build a `ChillOutConfig` from a parsed config block.

    Only the keys present in `block` are passed through to the model; absent
    keys fall back to the model's own defaults (with per-key gap-fill on
    `cooldown_days`). Unknown top-level keys raise `ConfigError` so user typos
    surface immediately instead of being silently ignored.
    """
    known_keys = {"cooldown", "include_groups", "fix_style"}
    unknown = sorted(set(block) - known_keys)
    ConfigError.require_condition(
        not unknown,
        f"Unknown configuration key(s) in {source}: {unknown}; valid keys are {sorted(known_keys)}",
    )

    kwargs: dict[str, Any] = {}
    if "cooldown" in block:
        kwargs["cooldown_days"] = ChillOutConfig.coerce_days(block["cooldown"])

    groups = ChillOutConfig.coerce_groups(block.get("include_groups"), source=source)
    if groups is not None:
        kwargs["include_groups"] = groups

    style = ChillOutConfig.coerce_fix_style(block.get("fix_style"), source=source)
    if style is not None:
        kwargs["fix_style"] = style

    with ConfigError.handle_errors(
        f"Invalid configuration in {source}",
        handle_exc_class=ValidationError,
    ):
        return ChillOutConfig(**kwargs)


def load_chill_out_file(root: Path) -> ChillOutConfig | None:
    """
    Load configuration from a dedicated `.chill-out.*` file at the project root.

    Searches for `.chill-out.yaml`, `.chill-out.yml`, `.chill-out.toml`, and
    `.chill-out.json`. Returns `None` when no such file exists. If more than
    one is present, raises `ConfigError` so the user resolves the ambiguity
    instead of silently picking one.
    """
    candidates = [root / f".chill-out{suffix}" for suffix in _CHILL_OUT_FILE_SUFFIXES]
    found = [p for p in candidates if p.is_file()]
    if not found:
        return None
    ConfigError.require_condition(
        len(found) == 1,
        f"Found multiple chill-out config files in {root}: {', '.join(p.name for p in found)}. Keep only one.",
    )
    path = found[0]
    doc = _parse_chill_out_file(path)
    return _config_from_block(doc, source=str(path))


def load_pyproject_table(root: Path) -> ChillOutConfig | None:
    """
    Load configuration from `[tool.chill-out]` in `pyproject.toml`.

    Returns `None` when `pyproject.toml` is missing or has no `[tool.chill-out]` table.
    """
    path = root / "pyproject.toml"
    if not path.is_file():
        return None

    with ConfigError.handle_errors(f"Failed to parse {path}"):
        doc = tomlkit.parse(path.read_text())

    with ConfigError.handle_errors(f"Malformed [tool] table in {path}"):
        raw_table = doc.get("tool", {}).get("chill-out")
    if raw_table is None:
        return None

    config_dict = ConfigError.ensure_type(
        raw_table,
        dict,
        message=f"[tool.chill-out] in {path} must be a table, got {type(raw_table).__name__}",
    )
    return _config_from_block(config_dict, source=str(path))


def load_package_json_block(root: Path) -> ChillOutConfig | None:
    """
    Load configuration from a top-level `"chill-out"` key in `package.json`.

    Returns `None` when `package.json` is missing or has no `"chill-out"` key.
    The key must contain an object with the same `"cooldown"`, `"include_groups"`,
    and `"fix_style"` sub-keys used by the pyproject and yaml sources, so the
    config shape stays identical across every source chill-out reads from:

    ```json
    {
      "chill-out": {
        "cooldown": {"major": 30, "minor": 14, "patch": 7, "default": 7},
        "include_groups": ["main", "dev"],
        "fix_style": "compatible"
      }
    }
    ```
    """
    path = root / "package.json"
    if not path.is_file():
        return None

    with ConfigError.handle_errors(f"Failed to parse {path}", handle_exc_class=json.JSONDecodeError):
        doc = json.loads(path.read_text())

    with ConfigError.handle_errors(f"Malformed top-level object in {path}"):
        raw_block = doc.get("chill-out")
    if raw_block is None:
        return None

    config_dict = ConfigError.ensure_type(
        raw_block,
        dict,
        message=f"'chill-out' in {path} must be an object, got {type(raw_block).__name__}",
    )
    return _config_from_block(config_dict, source=str(path))


def load_dependabot_cooldown(root: Path, ecosystem: EcosystemKind) -> ChillOutConfig | None:
    """
    Load cooldown thresholds from `.github/dependabot.yml` for the matching ecosystem.

    Returns `None` when no dependabot file exists, or when no update entry
    matches the given ecosystem. Dependabot has no concept of dependency-group
    filtering or fix style, so when a match is found this loader only ever
    supplies cooldown thresholds; the rest of `ChillOutConfig` falls back to
    its built-in defaults.

    Dependabot spells its cooldown keys `semver-major-days`, `semver-minor-days`,
    `semver-patch-days`, and `default-days`. They are translated here into the
    chill-out-native key names before handing off to `ChillOutConfig.coerce_days`,
    so the rest of the config layer never has to know dependabot's spelling.
    """
    path = root / ".github" / "dependabot.yml"
    if not path.is_file():
        return None

    with ConfigError.handle_errors(f"Failed to parse {path}", handle_exc_class=yaml.YAMLError):
        raw_doc = yaml.safe_load(path.read_text()) or {}

    doc = ConfigError.ensure_type(
        raw_doc,
        dict,
        message=f"Top-level content of {path} must be a mapping, got {type(raw_doc).__name__}",
    )

    target = "npm" if ecosystem is EcosystemKind.NPM else "pip"
    for raw_entry in doc.get("updates", []):
        entry = ConfigError.ensure_type(
            raw_entry,
            dict,
            message=f"Each entry under 'updates:' in {path} must be a mapping, got {type(raw_entry).__name__}",
        )
        if entry.get("package-ecosystem") != target:
            continue
        raw_cooldown = entry.get("cooldown", {})
        cooldown = ConfigError.ensure_type(
            raw_cooldown,
            dict,
            message=f"'cooldown:' in {path} must be a mapping, got {type(raw_cooldown).__name__}",
        )
        translated = _translate_dependabot_cooldown(cooldown)
        with ConfigError.handle_errors(
            f"Invalid configuration in {path}",
            handle_exc_class=ValidationError,
        ):
            return ChillOutConfig(cooldown_days=ChillOutConfig.coerce_days(translated))

    return None


_DEPENDABOT_COOLDOWN_KEYS: dict[str, str] = {
    "semver-major-days": "major",
    "semver-minor-days": "minor",
    "semver-patch-days": "patch",
    "default-days": "default",
}


def _translate_dependabot_cooldown(raw: dict[Any, Any]) -> dict[str, int]:
    """
    Rename dependabot's cooldown keys to chill-out's native spellings.

    Dependabot's `cooldown:` block also accepts `include` and `exclude` pattern lists
    that have no chill-out analog. Those (and any other non-day fields) are filtered
    out here so the strict `coerce_days` only sees keys it knows how to handle.
    """
    return {_DEPENDABOT_COOLDOWN_KEYS[str(k)]: v for k, v in raw.items() if str(k) in _DEPENDABOT_COOLDOWN_KEYS}


def _select_primary_source(root: Path) -> ChillOutConfig | None:
    """
    Pick the single chill-out-native config source for the project, or `None`.

    The chill-out-native sources are the dedicated `.chill-out.*` file, the
    `[tool.chill-out]` table in `pyproject.toml`, and the `chill-out` block in
    `package.json`. Two or more present at the same time is a `ConfigError`:
    chill-out refuses to guess which one wins.
    """
    candidates: list[tuple[str, ChillOutConfig]] = []
    for label, cfg in (
        (".chill-out.* file", load_chill_out_file(root)),
        ("[tool.chill-out] in pyproject.toml", load_pyproject_table(root)),
        ("chill-out block in package.json", load_package_json_block(root)),
    ):
        if cfg is not None:
            candidates.append((label, cfg))

    if not candidates:
        return None

    if len(candidates) > 1:
        names = ", ".join(label for label, _ in candidates)
        raise ConfigError(f"Found multiple chill-out config sources in {root}: {names}. Keep only one.")

    return candidates[0][1]


def load_config(root: Path, ecosystem: EcosystemKind) -> ChillOutConfig:
    """
    Resolve the effective chill-out configuration for the given project root and ecosystem.

    Picks a single config primary source, in priority order:
    - a chill-out-native source (dedicated `.chill-out.*` file
    - a `[tool.chill-out]` table in `pyproject.toml`
    - a `chill-out` block in `package.json`

    If no primary source is round, checks for cooldown config in `dependabot.yml`.

    If no config is found, use defaults.
    """
    return _select_primary_source(root) or load_dependabot_cooldown(root, ecosystem) or ChillOutConfig()

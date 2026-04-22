"""Unit tests for layered config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from chill_out.config import CooldownConfig, load_config
from chill_out.constants import DEFAULT_COOLDOWN_DAYS, BumpType, EcosystemKind
from chill_out.exceptions import ConfigError


class TestCooldownConfig:
    def test_for_bump_uses_explicit_value(self) -> None:
        cfg = CooldownConfig(days={BumpType.MAJOR: 60})
        assert cfg.for_bump(BumpType.MAJOR) == 60

    def test_for_bump_falls_back_to_default(self) -> None:
        cfg = CooldownConfig(days={BumpType.DEFAULT: 9})
        assert cfg.for_bump(BumpType.MINOR) == 9

    def test_for_bump_falls_back_to_hard_default_when_default_missing(self) -> None:
        cfg = CooldownConfig(days={BumpType.MAJOR: 60})
        assert cfg.for_bump(BumpType.MINOR) == DEFAULT_COOLDOWN_DAYS[BumpType.DEFAULT]


class TestLayering:
    def test_returns_defaults_when_no_sources(self, tmp_path: Path) -> None:
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        for bump, expected in DEFAULT_COOLDOWN_DAYS.items():
            assert cfg.for_bump(bump) == expected

    def test_dependabot_overrides_defaults(self, tmp_path: Path) -> None:
        gh = tmp_path / ".github"
        gh.mkdir()
        (gh / "dependabot.yml").write_text(
            "version: 2\n"
            "updates:\n"
            "  - package-ecosystem: npm\n"
            "    directory: '/'\n"
            "    schedule:\n"
            "      interval: weekly\n"
            "    cooldown:\n"
            "      semver-major-days: 99\n"
            "      semver-minor-days: 11\n"
        )
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_bump(BumpType.MAJOR) == 99
        assert cfg.for_bump(BumpType.MINOR) == 11
        # Patch falls back to default since dependabot didn't supply it.
        assert cfg.for_bump(BumpType.PATCH) == DEFAULT_COOLDOWN_DAYS[BumpType.PATCH]

    def test_dependabot_filters_by_ecosystem(self, tmp_path: Path) -> None:
        gh = tmp_path / ".github"
        gh.mkdir()
        (gh / "dependabot.yml").write_text(
            "version: 2\n"
            "updates:\n"
            "  - package-ecosystem: npm\n"
            "    cooldown:\n"
            "      semver-major-days: 99\n"
            "  - package-ecosystem: pip\n"
            "    cooldown:\n"
            "      semver-major-days: 42\n"
        )
        npm_cfg = load_config(tmp_path, EcosystemKind.NPM)
        pypi_cfg = load_config(tmp_path, EcosystemKind.PYPI)
        assert npm_cfg.for_bump(BumpType.MAJOR) == 99
        assert pypi_cfg.for_bump(BumpType.MAJOR) == 42

    def test_pyproject_overrides_dependabot(self, tmp_path: Path) -> None:
        gh = tmp_path / ".github"
        gh.mkdir()
        (gh / "dependabot.yml").write_text(
            "updates:\n"
            "  - package-ecosystem: npm\n"
            "    cooldown:\n"
            "      semver-major-days: 99\n"
        )
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            '[tool.chill-out.cooldown]\nmajor = 7\n'
        )
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_bump(BumpType.MAJOR) == 7

    def test_yaml_overrides_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            '[tool.chill-out.cooldown]\nmajor = 7\n'
        )
        (tmp_path / ".chill-out.yaml").write_text("cooldown:\n  major: 3\n")
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_bump(BumpType.MAJOR) == 3

    def test_yml_extension_works(self, tmp_path: Path) -> None:
        (tmp_path / ".chill-out.yml").write_text("cooldown:\n  major: 4\n")
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_bump(BumpType.MAJOR) == 4

    def test_invalid_yaml_raises_config_error(self, tmp_path: Path) -> None:
        (tmp_path / ".chill-out.yaml").write_text("cooldown:\n  major: [unbalanced\n")
        with pytest.raises(ConfigError):
            load_config(tmp_path, EcosystemKind.NPM)

    def test_non_int_value_raises(self, tmp_path: Path) -> None:
        (tmp_path / ".chill-out.yaml").write_text('cooldown:\n  major: "lots"\n')
        with pytest.raises(ConfigError):
            load_config(tmp_path, EcosystemKind.NPM)

    def test_unknown_keys_are_ignored(self, tmp_path: Path) -> None:
        (tmp_path / ".chill-out.yaml").write_text("cooldown:\n  weird: 100\n  major: 2\n")
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_bump(BumpType.MAJOR) == 2

    def test_missing_cooldown_block_returns_defaults(self, tmp_path: Path) -> None:
        (tmp_path / ".chill-out.yaml").write_text("other: thing\n")
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_bump(BumpType.MAJOR) == DEFAULT_COOLDOWN_DAYS[BumpType.MAJOR]

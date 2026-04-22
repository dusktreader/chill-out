"""Unit tests for layered config loading."""

from __future__ import annotations

from pathlib import Path

import pytest
from chill_out.config import CooldownConfig, load_config
from chill_out.constants import DEFAULT_COOLDOWN_DAYS, ReleaseType, EcosystemKind
from chill_out.exceptions import ConfigError


class TestCooldownConfig:
    def test_for_release_type_uses_explicit_value(self) -> None:
        cfg = CooldownConfig(cooldown_days={ReleaseType.MAJOR: 60})
        assert cfg.for_release_type(ReleaseType.MAJOR) == 60

    def test_for_release_type_falls_back_to_default(self) -> None:
        cfg = CooldownConfig(cooldown_days={ReleaseType.DEFAULT: 9})
        assert cfg.for_release_type(ReleaseType.MINOR) == 9

    def test_for_release_type_falls_back_to_hard_default_when_default_missing(self) -> None:
        cfg = CooldownConfig(cooldown_days={ReleaseType.MAJOR: 60})
        assert cfg.for_release_type(ReleaseType.MINOR) == DEFAULT_COOLDOWN_DAYS[ReleaseType.DEFAULT]


class TestLayering:
    def test_returns_defaults_when_no_sources(self, tmp_path: Path) -> None:
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        for rel_type, expected in DEFAULT_COOLDOWN_DAYS.items():
            assert cfg.for_release_type(rel_type) == expected

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
        assert cfg.for_release_type(ReleaseType.MAJOR) == 99
        assert cfg.for_release_type(ReleaseType.MINOR) == 11
        # Patch falls back to default since dependabot didn't supply it.
        assert cfg.for_release_type(ReleaseType.PATCH) == DEFAULT_COOLDOWN_DAYS[ReleaseType.PATCH]

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
        assert npm_cfg.for_release_type(ReleaseType.MAJOR) == 99
        assert pypi_cfg.for_release_type(ReleaseType.MAJOR) == 42

    def test_pyproject_overrides_dependabot(self, tmp_path: Path) -> None:
        gh = tmp_path / ".github"
        gh.mkdir()
        (gh / "dependabot.yml").write_text(
            "updates:\n  - package-ecosystem: npm\n    cooldown:\n      semver-major-days: 99\n"
        )
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0"\n[tool.chill-out.cooldown]\nmajor = 7\n'
        )
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == 7

    def test_yaml_overrides_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0"\n[tool.chill-out.cooldown]\nmajor = 7\n'
        )
        (tmp_path / ".chill-out.yaml").write_text("cooldown:\n  major: 3\n")
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == 3

    def test_yml_extension_works(self, tmp_path: Path) -> None:
        (tmp_path / ".chill-out.yml").write_text("cooldown:\n  major: 4\n")
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == 4

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
        assert cfg.for_release_type(ReleaseType.MAJOR) == 2

    def test_missing_cooldown_block_returns_defaults(self, tmp_path: Path) -> None:
        (tmp_path / ".chill-out.yaml").write_text("other: thing\n")
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == DEFAULT_COOLDOWN_DAYS[ReleaseType.MAJOR]


class TestPackageJsonSource:
    def test_nested_cooldown_key_loads(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            '{"name":"x","version":"1.0.0","chill-out":{"cooldown":{"major":21,"minor":5}}}'
        )
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == 21
        assert cfg.for_release_type(ReleaseType.MINOR) == 5

    def test_flat_chill_out_key_loads(self, tmp_path: Path) -> None:
        # The cooldown sub-key is optional; a flat map works too.
        (tmp_path / "package.json").write_text(
            '{"name":"x","version":"1.0.0","chill-out":{"major":15,"patch":2}}'
        )
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == 15
        assert cfg.for_release_type(ReleaseType.PATCH) == 2

    def test_missing_chill_out_key_returns_defaults(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"name":"x","version":"1.0.0"}')
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == DEFAULT_COOLDOWN_DAYS[ReleaseType.MAJOR]

    def test_overrides_dependabot(self, tmp_path: Path) -> None:
        gh = tmp_path / ".github"
        gh.mkdir()
        (gh / "dependabot.yml").write_text(
            "updates:\n  - package-ecosystem: npm\n    cooldown:\n      semver-major-days: 99\n"
        )
        (tmp_path / "package.json").write_text(
            '{"name":"x","version":"1.0.0","chill-out":{"cooldown":{"major":3}}}'
        )
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == 3

    def test_yaml_overrides_package_json(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            '{"name":"x","version":"1.0.0","chill-out":{"cooldown":{"major":3}}}'
        )
        (tmp_path / ".chill-out.yaml").write_text("cooldown:\n  major: 1\n")
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == 1

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"name": "x", broken')
        with pytest.raises(ConfigError):
            load_config(tmp_path, EcosystemKind.NPM)

    def test_non_dict_chill_out_value_is_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"name":"x","version":"1.0.0","chill-out":"oops"}')
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == DEFAULT_COOLDOWN_DAYS[ReleaseType.MAJOR]


class TestIncludeGroups:
    """Loading and merging behavior for include_groups across config sources."""

    def test_default_is_main_only(self, tmp_path: Path) -> None:
        from chill_out.constants import DependencyGroup

        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.include_groups == (DependencyGroup.MAIN,)

    def test_chill_out_yaml_sets_include_groups(self, tmp_path: Path) -> None:
        from chill_out.constants import DependencyGroup

        (tmp_path / ".chill-out.yaml").write_text(
            "include_groups: [main, dev]\n"
        )
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.include_groups == (DependencyGroup.MAIN, DependencyGroup.DEV)

    def test_pyproject_table_sets_include_groups(self, tmp_path: Path) -> None:
        from chill_out.constants import DependencyGroup

        (tmp_path / "pyproject.toml").write_text(
            '[tool.chill-out]\ninclude_groups = ["main", "optional"]\n'
        )
        cfg = load_config(tmp_path, EcosystemKind.PYPI)
        assert cfg.include_groups == (DependencyGroup.MAIN, DependencyGroup.OPTIONAL)

    def test_yaml_overrides_pyproject_for_groups(self, tmp_path: Path) -> None:
        from chill_out.constants import DependencyGroup

        (tmp_path / "pyproject.toml").write_text(
            '[tool.chill-out]\ninclude_groups = ["main"]\n'
        )
        (tmp_path / ".chill-out.yaml").write_text("include_groups: [dev]\n")
        cfg = load_config(tmp_path, EcosystemKind.PYPI)
        # The higher-priority yaml wins wholesale; it's not unioned with pyproject's value.
        assert cfg.include_groups == (DependencyGroup.DEV,)

    def test_unknown_group_raises_config_error(self, tmp_path: Path) -> None:
        (tmp_path / ".chill-out.yaml").write_text("include_groups: [bogus]\n")
        with pytest.raises(ConfigError, match="Unknown dependency group"):
            load_config(tmp_path, EcosystemKind.NPM)

    def test_non_list_raises_config_error(self, tmp_path: Path) -> None:
        (tmp_path / ".chill-out.yaml").write_text('include_groups: "main"\n')
        with pytest.raises(ConfigError, match="must be a list"):
            load_config(tmp_path, EcosystemKind.NPM)

    def test_explicit_empty_list_is_respected(self, tmp_path: Path) -> None:
        (tmp_path / ".chill-out.yaml").write_text("include_groups: []\n")
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.include_groups == ()


class TestFixStyleConfig:
    """Layered loading and validation for the ``fix_style`` setting."""

    def test_default_is_exact(self, tmp_path: Path) -> None:
        from chill_out.constants import FixStyle

        cfg = load_config(tmp_path, EcosystemKind.PYPI)
        assert cfg.fix_style is FixStyle.EXACT

    def test_yaml_can_set_compatible(self, tmp_path: Path) -> None:
        from chill_out.constants import FixStyle

        (tmp_path / ".chill-out.yaml").write_text("fix_style: compatible\n")
        cfg = load_config(tmp_path, EcosystemKind.PYPI)
        assert cfg.fix_style is FixStyle.COMPATIBLE

    def test_pyproject_can_set_compatible(self, tmp_path: Path) -> None:
        from chill_out.constants import FixStyle

        (tmp_path / "pyproject.toml").write_text(
            '[tool.chill-out]\nfix_style = "compatible"\n'
        )
        cfg = load_config(tmp_path, EcosystemKind.PYPI)
        assert cfg.fix_style is FixStyle.COMPATIBLE

    def test_package_json_can_set_compatible(self, tmp_path: Path) -> None:
        import json as _json

        from chill_out.constants import FixStyle

        (tmp_path / "package.json").write_text(
            _json.dumps({"chill-out": {"fix_style": "compatible"}})
        )
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.fix_style is FixStyle.COMPATIBLE

    def test_yaml_overrides_pyproject(self, tmp_path: Path) -> None:
        from chill_out.constants import FixStyle

        (tmp_path / "pyproject.toml").write_text('[tool.chill-out]\nfix_style = "exact"\n')
        (tmp_path / ".chill-out.yaml").write_text("fix_style: compatible\n")
        cfg = load_config(tmp_path, EcosystemKind.PYPI)
        # Highest-priority source wins wholesale.
        assert cfg.fix_style is FixStyle.COMPATIBLE

    def test_unknown_style_raises_config_error(self, tmp_path: Path) -> None:
        (tmp_path / ".chill-out.yaml").write_text("fix_style: aggressive\n")
        with pytest.raises(ConfigError, match="Unknown fix_style"):
            load_config(tmp_path, EcosystemKind.PYPI)

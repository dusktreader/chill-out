"""Unit tests for chill-out config loading and source selection."""

from pathlib import Path

import pytest
from chill_out.config import ChillOutConfig, load_config
from chill_out.constants import DEFAULT_COOLDOWN_DAYS, EcosystemKind, ReleaseType
from chill_out.exceptions import ConfigError


class TestChillOutConfig:
    def test_for_release_type_uses_explicit_value(self) -> None:
        cfg = ChillOutConfig(cooldown_days={ReleaseType.MAJOR: 60})
        assert cfg.for_release_type(ReleaseType.MAJOR) == 60

    def test_for_release_type_gap_fills_unset_keys_from_defaults(self) -> None:
        cfg = ChillOutConfig(cooldown_days={ReleaseType.MAJOR: 60})
        assert cfg.for_release_type(ReleaseType.MAJOR) == 60
        assert cfg.for_release_type(ReleaseType.MINOR) == DEFAULT_COOLDOWN_DAYS[ReleaseType.MINOR]
        assert cfg.for_release_type(ReleaseType.PATCH) == DEFAULT_COOLDOWN_DAYS[ReleaseType.PATCH]
        assert cfg.for_release_type(ReleaseType.DEFAULT) == DEFAULT_COOLDOWN_DAYS[ReleaseType.DEFAULT]

    def test_for_release_type_explicit_default_does_not_override_other_keys(self) -> None:
        cfg = ChillOutConfig(cooldown_days={ReleaseType.DEFAULT: 9})
        assert cfg.for_release_type(ReleaseType.DEFAULT) == 9
        assert cfg.for_release_type(ReleaseType.MINOR) == DEFAULT_COOLDOWN_DAYS[ReleaseType.MINOR]


class TestSourceSelection:
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

    def test_dependabot_strips_include_exclude_patterns(self, tmp_path: Path) -> None:
        # Dependabot's cooldown block also takes include/exclude pattern lists
        # that have no chill-out analog. The loader must drop them before they
        # reach the strict day-key coercer.
        gh = tmp_path / ".github"
        gh.mkdir()
        (gh / "dependabot.yml").write_text(
            "version: 2\n"
            "updates:\n"
            "  - package-ecosystem: npm\n"
            "    cooldown:\n"
            "      semver-major-days: 50\n"
            "      include:\n"
            "        - 'react*'\n"
            "      exclude:\n"
            "        - 'lodash'\n"
        )
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == 50

    def test_dependabot_non_mapping_top_level_raises(self, tmp_path: Path) -> None:
        gh = tmp_path / ".github"
        gh.mkdir()
        (gh / "dependabot.yml").write_text("- not\n- a\n- mapping\n")
        with pytest.raises(ConfigError, match="must be a mapping"):
            load_config(tmp_path, EcosystemKind.NPM)

    def test_dependabot_non_mapping_update_entry_raises(self, tmp_path: Path) -> None:
        gh = tmp_path / ".github"
        gh.mkdir()
        (gh / "dependabot.yml").write_text("updates:\n  - 'oops'\n")
        with pytest.raises(ConfigError, match="Each entry under 'updates:'"):
            load_config(tmp_path, EcosystemKind.NPM)

    def test_dependabot_non_mapping_cooldown_raises(self, tmp_path: Path) -> None:
        gh = tmp_path / ".github"
        gh.mkdir()
        (gh / "dependabot.yml").write_text("updates:\n  - package-ecosystem: npm\n    cooldown: 7\n")
        with pytest.raises(ConfigError, match="'cooldown:' in .* must be a mapping"):
            load_config(tmp_path, EcosystemKind.NPM)

    def test_dependabot_returns_defaults_when_no_matching_ecosystem(self, tmp_path: Path) -> None:
        """When `dependabot.yml` only mentions other ecosystems, the loader yields built-in defaults."""
        from chill_out.constants import DEFAULT_COOLDOWN_DAYS

        gh = tmp_path / ".github"
        gh.mkdir()
        (gh / "dependabot.yml").write_text(
            "updates:\n  - package-ecosystem: docker\n    cooldown:\n      semver-major-days: 99\n"
        )
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == DEFAULT_COOLDOWN_DAYS[ReleaseType.MAJOR]

    def test_coerce_days_returns_empty_dict_for_none(self) -> None:
        """`ChillOutConfig.coerce_days(None)` short-circuits to an empty dict."""
        from chill_out.config import ChillOutConfig

        assert ChillOutConfig.coerce_days(None) == {}

    def test_pyproject_takes_precedence_over_dependabot(self, tmp_path: Path) -> None:
        # When a chill-out-native source exists, dependabot is ignored entirely.
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

    def test_yaml_and_pyproject_together_raises(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0"\n[tool.chill-out.cooldown]\nmajor = 7\n'
        )
        (tmp_path / ".chill-out.yaml").write_text("cooldown:\n  major: 3\n")
        with pytest.raises(ConfigError, match="multiple chill-out config sources"):
            load_config(tmp_path, EcosystemKind.NPM)

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

    def test_unknown_keys_raise(self, tmp_path: Path) -> None:
        (tmp_path / ".chill-out.yaml").write_text("cooldown:\n  weird: 100\n  major: 2\n")
        with pytest.raises(ConfigError, match="Unknown cooldown key 'weird'"):
            load_config(tmp_path, EcosystemKind.NPM)

    def test_non_mapping_top_level_raises(self, tmp_path: Path) -> None:
        # A YAML file whose top level isn't a mapping (e.g. a bare list) is a
        # user mistake, not a "no config here" signal. The loader must surface it.
        (tmp_path / ".chill-out.yaml").write_text("- major: 7\n- minor: 3\n")
        with pytest.raises(ConfigError, match="must be a mapping"):
            load_config(tmp_path, EcosystemKind.NPM)

    def test_non_mapping_cooldown_block_raises(self, tmp_path: Path) -> None:
        # cooldown: must be a mapping. A scalar or list is malformed config,
        # not "absent config", so the loader has to fail loudly.
        (tmp_path / ".chill-out.yaml").write_text("cooldown: 7\n")
        with pytest.raises(ConfigError, match="cooldown must be a mapping"):
            load_config(tmp_path, EcosystemKind.NPM)

    def test_pyproject_tool_chill_out_not_a_table_raises(self, tmp_path: Path) -> None:
        # `tool.chill-out = "broken"` (a string instead of a table) is a typo
        # waiting to bite. The loader must surface it instead of silently
        # falling back to defaults.
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0"\n[tool]\nchill-out = "broken"\n')
        with pytest.raises(ConfigError, match=r"\[tool.chill-out\].*must be a table"):
            load_config(tmp_path, EcosystemKind.PYPI)

    def test_missing_cooldown_block_returns_defaults(self, tmp_path: Path) -> None:
        # An empty config file is valid; every field falls back to its default.
        (tmp_path / ".chill-out.yaml").write_text("{}\n")
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == DEFAULT_COOLDOWN_DAYS[ReleaseType.MAJOR]

    def test_unknown_block_keys_raise(self, tmp_path: Path) -> None:
        # Unknown keys at the chill-out block level are typos in disguise; the
        # strict-config loader has to surface them.
        (tmp_path / ".chill-out.yaml").write_text("other: thing\n")
        with pytest.raises(ConfigError, match="Unknown configuration key"):
            load_config(tmp_path, EcosystemKind.NPM)

    def test_pyproject_and_package_json_together_raises(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[tool.chill-out.cooldown]\nmajor = 7\n")
        (tmp_path / "package.json").write_text('{"name":"x","version":"1.0.0","chill-out":{"cooldown":{"major":3}}}')
        with pytest.raises(ConfigError, match="multiple chill-out config sources"):
            load_config(tmp_path, EcosystemKind.NPM)

    def test_partial_source_fills_other_fields_from_defaults(self, tmp_path: Path) -> None:
        # A source that only sets cooldown still gets default include_groups and fix_style.
        from chill_out.constants import DEFAULT_FIX_STYLE, DEFAULT_INCLUDE_GROUPS

        (tmp_path / ".chill-out.yaml").write_text("cooldown:\n  major: 42\n")
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == 42
        # Per-key gap-fill: untouched release types come from DEFAULT_COOLDOWN_DAYS.
        assert cfg.for_release_type(ReleaseType.PATCH) == DEFAULT_COOLDOWN_DAYS[ReleaseType.PATCH]
        assert cfg.include_groups == DEFAULT_INCLUDE_GROUPS
        assert cfg.fix_style is DEFAULT_FIX_STYLE


class TestDedicatedFileFormats:
    """The dedicated `.chill-out.*` config file accepts yaml, yml, toml, and json."""

    def test_toml_extension_works(self, tmp_path: Path) -> None:
        (tmp_path / ".chill-out.toml").write_text("[cooldown]\nmajor = 8\n")
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == 8

    def test_json_extension_works(self, tmp_path: Path) -> None:
        (tmp_path / ".chill-out.json").write_text('{"cooldown": {"major": 6}}')
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == 6

    def test_toml_supports_include_groups_and_fix_style(self, tmp_path: Path) -> None:
        from chill_out.constants import DependencyGroup, FixStyle

        (tmp_path / ".chill-out.toml").write_text(
            'include_groups = ["main", "dev"]\nfix_style = "compatible"\n\n[cooldown]\nmajor = 12\n'
        )
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == 12
        assert cfg.include_groups == (DependencyGroup.MAIN, DependencyGroup.DEV)
        assert cfg.fix_style is FixStyle.COMPATIBLE

    def test_json_supports_include_groups_and_fix_style(self, tmp_path: Path) -> None:
        from chill_out.constants import DependencyGroup, FixStyle

        (tmp_path / ".chill-out.json").write_text(
            '{"cooldown": {"major": 9}, "include_groups": ["main", "optional"], "fix_style": "compatible"}'
        )
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == 9
        assert cfg.include_groups == (DependencyGroup.MAIN, DependencyGroup.OPTIONAL)
        assert cfg.fix_style is FixStyle.COMPATIBLE

    def test_invalid_toml_raises_config_error(self, tmp_path: Path) -> None:
        (tmp_path / ".chill-out.toml").write_text("not = valid = toml\n")
        with pytest.raises(ConfigError, match="Failed to parse"):
            load_config(tmp_path, EcosystemKind.NPM)

    def test_invalid_json_raises_config_error(self, tmp_path: Path) -> None:
        (tmp_path / ".chill-out.json").write_text("{not valid json}")
        with pytest.raises(ConfigError, match="Failed to parse"):
            load_config(tmp_path, EcosystemKind.NPM)

    def test_empty_json_returns_defaults(self, tmp_path: Path) -> None:
        (tmp_path / ".chill-out.json").write_text("")
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == DEFAULT_COOLDOWN_DAYS[ReleaseType.MAJOR]

    def test_multiple_files_raises_config_error(self, tmp_path: Path) -> None:
        (tmp_path / ".chill-out.yaml").write_text("cooldown:\n  major: 1\n")
        (tmp_path / ".chill-out.toml").write_text("[cooldown]\nmajor = 2\n")
        with pytest.raises(ConfigError, match="multiple chill-out config files"):
            load_config(tmp_path, EcosystemKind.NPM)


class TestPackageJsonSource:
    def test_nested_cooldown_key_loads(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            '{"name":"x","version":"1.0.0","chill-out":{"cooldown":{"major":21,"minor":5}}}'
        )
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == 21
        assert cfg.for_release_type(ReleaseType.MINOR) == 5

    def test_flat_chill_out_key_no_longer_supported(self, tmp_path: Path) -> None:
        # The flat shape (cooldown days at the top of the chill-out block) was
        # dropped to keep one canonical config shape across every source. A
        # user who tries it should get a clear error from the strict-config
        # path, not a silent acceptance.
        (tmp_path / "package.json").write_text('{"name":"x","version":"1.0.0","chill-out":{"major":15,"patch":2}}')
        with pytest.raises(ConfigError, match="Unknown configuration key"):
            load_config(tmp_path, EcosystemKind.NPM)

    def test_missing_chill_out_key_returns_defaults(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"name":"x","version":"1.0.0"}')
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == DEFAULT_COOLDOWN_DAYS[ReleaseType.MAJOR]

    def test_takes_precedence_over_dependabot(self, tmp_path: Path) -> None:
        # When package.json carries a chill-out block, dependabot is ignored entirely.
        gh = tmp_path / ".github"
        gh.mkdir()
        (gh / "dependabot.yml").write_text(
            "updates:\n  - package-ecosystem: npm\n    cooldown:\n      semver-major-days: 99\n"
        )
        (tmp_path / "package.json").write_text('{"name":"x","version":"1.0.0","chill-out":{"cooldown":{"major":3}}}')
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.for_release_type(ReleaseType.MAJOR) == 3

    def test_yaml_and_package_json_together_raises(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"name":"x","version":"1.0.0","chill-out":{"cooldown":{"major":3}}}')
        (tmp_path / ".chill-out.yaml").write_text("cooldown:\n  major: 1\n")
        with pytest.raises(ConfigError, match="multiple chill-out config sources"):
            load_config(tmp_path, EcosystemKind.NPM)

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"name": "x", broken')
        with pytest.raises(ConfigError):
            load_config(tmp_path, EcosystemKind.NPM)

    def test_non_dict_chill_out_value_raises(self, tmp_path: Path) -> None:
        # `"chill-out": "oops"` is malformed config, not absent config.
        (tmp_path / "package.json").write_text('{"name":"x","version":"1.0.0","chill-out":"oops"}')
        with pytest.raises(ConfigError, match="'chill-out' in .* must be an object"):
            load_config(tmp_path, EcosystemKind.NPM)

    def test_non_object_top_level_raises(self, tmp_path: Path) -> None:
        # A package.json whose top level isn't an object is malformed npm config.
        (tmp_path / "package.json").write_text('["not", "an", "object"]')
        with pytest.raises(ConfigError, match="Malformed top-level object"):
            load_config(tmp_path, EcosystemKind.NPM)


class TestIncludeGroups:
    """Loading and merging behavior for include_groups across config sources."""

    def test_default_is_main_only(self, tmp_path: Path) -> None:
        from chill_out.constants import DependencyGroup

        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.include_groups == (DependencyGroup.MAIN,)

    def test_chill_out_yaml_sets_include_groups(self, tmp_path: Path) -> None:
        from chill_out.constants import DependencyGroup

        (tmp_path / ".chill-out.yaml").write_text("include_groups: [main, dev]\n")
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.include_groups == (DependencyGroup.MAIN, DependencyGroup.DEV)

    def test_pyproject_table_sets_include_groups(self, tmp_path: Path) -> None:
        from chill_out.constants import DependencyGroup

        (tmp_path / "pyproject.toml").write_text('[tool.chill-out]\ninclude_groups = ["main", "optional"]\n')
        cfg = load_config(tmp_path, EcosystemKind.PYPI)
        assert cfg.include_groups == (DependencyGroup.MAIN, DependencyGroup.OPTIONAL)

    def test_yaml_and_pyproject_groups_together_raises(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[tool.chill-out]\ninclude_groups = ["main"]\n')
        (tmp_path / ".chill-out.yaml").write_text("include_groups: [dev]\n")
        with pytest.raises(ConfigError, match="multiple chill-out config sources"):
            load_config(tmp_path, EcosystemKind.PYPI)

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
    """Layered loading and validation for the `fix_style` setting."""

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

        (tmp_path / "pyproject.toml").write_text('[tool.chill-out]\nfix_style = "compatible"\n')
        cfg = load_config(tmp_path, EcosystemKind.PYPI)
        assert cfg.fix_style is FixStyle.COMPATIBLE

    def test_package_json_can_set_compatible(self, tmp_path: Path) -> None:
        import json as _json

        from chill_out.constants import FixStyle

        (tmp_path / "package.json").write_text(_json.dumps({"chill-out": {"fix_style": "compatible"}}))
        cfg = load_config(tmp_path, EcosystemKind.NPM)
        assert cfg.fix_style is FixStyle.COMPATIBLE

    def test_yaml_and_pyproject_fix_style_together_raises(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[tool.chill-out]\nfix_style = "exact"\n')
        (tmp_path / ".chill-out.yaml").write_text("fix_style: compatible\n")
        with pytest.raises(ConfigError, match="multiple chill-out config sources"):
            load_config(tmp_path, EcosystemKind.PYPI)

    def test_unknown_style_raises_config_error(self, tmp_path: Path) -> None:
        (tmp_path / ".chill-out.yaml").write_text("fix_style: aggressive\n")
        with pytest.raises(ConfigError, match="Unknown fix_style"):
            load_config(tmp_path, EcosystemKind.PYPI)


class TestValidatorDefaults:
    """Direct tests of the field-validator default branches."""

    def test_explicit_none_include_groups_yields_default(self) -> None:
        """Passing `include_groups=None` falls back to `DEFAULT_INCLUDE_GROUPS`."""
        from chill_out.constants import DEFAULT_INCLUDE_GROUPS

        cfg = ChillOutConfig(include_groups=None)  # ty: ignore[invalid-argument-type]
        assert cfg.include_groups == DEFAULT_INCLUDE_GROUPS

    def test_explicit_none_fix_style_yields_default(self) -> None:
        """Passing `fix_style=None` falls back to `DEFAULT_FIX_STYLE`."""
        from chill_out.constants import DEFAULT_FIX_STYLE

        cfg = ChillOutConfig(fix_style=None)  # ty: ignore[invalid-argument-type]
        assert cfg.fix_style is DEFAULT_FIX_STYLE

"""Integration tests: exercise the CLI end-to-end against fixture projects.

These intentionally avoid mocking individual functions; instead they mock at the
HTTP boundary (via ``respx``) and at the package-manager subprocess boundary.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pendulum
import pytest
import respx
from typer.testing import CliRunner

from chill_out.cli.main import cli
from chill_out.constants import ExitCode
from chill_out.ecosystems.npm import NPM_REGISTRY
from chill_out.ecosystems.pypi import PYPI_REGISTRY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> pendulum.DateTime:
    return pendulum.now("UTC")


def _iso(days_ago: int) -> str:
    return _now().subtract(days=days_ago).to_iso8601_string()


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# Pypi end-to-end
# ---------------------------------------------------------------------------


@pytest.fixture
def pypi_root(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\n'
        'name = "fixture"\n'
        'version = "0.1.0"\n'
        'dependencies = ["fastdep==2.0.0", "olddep==1.0.0"]\n'
    )
    (tmp_path / "uv.lock").write_text(
        'version = 1\n'
        '[[package]]\n'
        'name = "fastdep"\n'
        'version = "2.0.0"\n'
        '\n'
        '[[package]]\n'
        'name = "olddep"\n'
        'version = "1.0.0"\n'
    )
    return tmp_path


@respx.mock
def test_pypi_check_reports_violations_and_exits_nonzero(
    pypi_root: Path, runner: CliRunner
) -> None:
    """A package published yesterday should violate cooldown; an old package should pass."""
    respx.get(f"{PYPI_REGISTRY}/fastdep/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "releases": {
                    "2.0.0": [{"upload_time_iso_8601": _iso(1)}],
                    "1.5.0": [{"upload_time_iso_8601": _iso(200)}],
                }
            },
        )
    )
    respx.get(f"{PYPI_REGISTRY}/olddep/json").mock(
        return_value=httpx.Response(
            200,
            json={"releases": {"1.0.0": [{"upload_time_iso_8601": _iso(400)}]}},
        )
    )

    result = runner.invoke(cli, ["check", "--root", str(pypi_root), "--quiet"])
    assert result.exit_code == int(ExitCode.COOLDOWN_VIOLATION)
    assert "fastdep" in result.stdout
    assert "1.5.0" in result.stdout  # safe rollback suggested
    assert "olddep" not in result.stdout  # passing pkg not in the violation table


@respx.mock
def test_pypi_check_succeeds_when_all_deps_are_old(pypi_root: Path, runner: CliRunner) -> None:
    respx.get(f"{PYPI_REGISTRY}/fastdep/json").mock(
        return_value=httpx.Response(
            200, json={"releases": {"2.0.0": [{"upload_time_iso_8601": _iso(400)}]}}
        )
    )
    respx.get(f"{PYPI_REGISTRY}/olddep/json").mock(
        return_value=httpx.Response(
            200, json={"releases": {"1.0.0": [{"upload_time_iso_8601": _iso(400)}]}}
        )
    )
    result = runner.invoke(cli, ["check", "--root", str(pypi_root), "--quiet"])
    assert result.exit_code == 0
    assert "No cooldown violations" in result.stdout


@respx.mock
def test_pypi_fix_pins_violating_dep_and_calls_uv_lock(
    pypi_root: Path, runner: CliRunner
) -> None:
    respx.get(f"{PYPI_REGISTRY}/fastdep/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "releases": {
                    "2.0.0": [{"upload_time_iso_8601": _iso(1)}],
                    "1.5.0": [{"upload_time_iso_8601": _iso(200)}],
                }
            },
        )
    )
    respx.get(f"{PYPI_REGISTRY}/olddep/json").mock(
        return_value=httpx.Response(
            200, json={"releases": {"1.0.0": [{"upload_time_iso_8601": _iso(400)}]}}
        )
    )

    fake_uv = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    with patch("chill_out.ecosystems.pypi.subprocess.run", return_value=fake_uv) as run_mock:
        result = runner.invoke(cli, ["check", "--root", str(pypi_root), "--quiet", "--fix"])

    assert run_mock.called
    contents = (pypi_root / "pyproject.toml").read_text()
    assert "fastdep==1.5.0" in contents
    assert "olddep==1.0.0" in contents
    assert result.exit_code == int(ExitCode.COOLDOWN_VIOLATION)


@respx.mock
def test_pypi_fast_mode_omits_safe_version(pypi_root: Path, runner: CliRunner) -> None:
    respx.get(f"{PYPI_REGISTRY}/fastdep/json").mock(
        return_value=httpx.Response(
            200, json={"releases": {"2.0.0": [{"upload_time_iso_8601": _iso(1)}]}}
        )
    )
    respx.get(f"{PYPI_REGISTRY}/olddep/json").mock(
        return_value=httpx.Response(
            200, json={"releases": {"1.0.0": [{"upload_time_iso_8601": _iso(400)}]}}
        )
    )
    result = runner.invoke(cli, ["check", "--root", str(pypi_root), "--quiet", "--fast"])
    assert result.exit_code == int(ExitCode.COOLDOWN_VIOLATION)
    assert "Suggested" not in result.stdout


# ---------------------------------------------------------------------------
# npm end-to-end
# ---------------------------------------------------------------------------


@pytest.fixture
def npm_root(tmp_path: Path) -> Path:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "fixture",
                "version": "0.1.0",
                "dependencies": {"left-pad": "^1.0.0"},
            },
            indent=2,
        )
    )
    return tmp_path


@respx.mock
def test_npm_check_reports_violation(npm_root: Path, runner: CliRunner) -> None:
    respx.get(f"{NPM_REGISTRY}/left-pad").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "left-pad",
                "time": {"1.3.0": _iso(1), "1.2.0": _iso(200)},
            },
        )
    )

    fake_list = {
        "returncode": 0,
        "stdout": json.dumps({"dependencies": {"left-pad": {"version": "1.3.0"}}}),
        "stderr": "",
    }

    def fake_run(cmd, **kw):
        return type("R", (), fake_list)()

    with patch("chill_out.ecosystems.npm.subprocess.run", side_effect=fake_run):
        result = runner.invoke(cli, ["check", "--root", str(npm_root), "--quiet"])
    assert result.exit_code == int(ExitCode.COOLDOWN_VIOLATION)
    assert "left-pad" in result.stdout
    assert "1.2.0" in result.stdout


# ---------------------------------------------------------------------------
# Auto-detection
# ---------------------------------------------------------------------------


def test_check_fails_clearly_with_no_recognised_ecosystem(
    tmp_path: Path, runner: CliRunner
) -> None:
    result = runner.invoke(cli, ["check", "--root", str(tmp_path), "--quiet"])
    assert result.exit_code != 0
    # The error reaches stdout via the rich console (typer captures both streams).
    combined = result.stdout + (result.stderr or "")
    assert "ecosystem" in combined.lower() or "could not detect" in combined.lower()


def test_show_config_works_for_npm(npm_root: Path, runner: CliRunner) -> None:
    (npm_root / ".chill-out.yaml").write_text("cooldown:\n  major: 99\n")
    result = runner.invoke(cli, ["show-config", "--root", str(npm_root)])
    assert result.exit_code == 0
    assert "99" in result.stdout

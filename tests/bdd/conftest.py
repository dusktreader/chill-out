"""Shared fixtures and step definitions for BDD scenarios.

The BDD layer exercises chill-out from the user's perspective: a project
directory on disk, a CLI invocation, and observable output. HTTP traffic
is mocked at the boundary with respx so the tests stay offline; the
package-manager subprocess (uv, npm) is mocked the same way the existing
integration tests mock it.

All Given/When/Then bindings shared across feature files live here.
Per-feature bindings live in the matching test_*_steps.py module.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import httpx
import pendulum
import pytest
import respx
from chill_out.cli.main import cli
from chill_out.ecosystems.npm import NPM_REGISTRY
from chill_out.ecosystems.pypi import PYPI_REGISTRY
from pytest_bdd import given, parsers, then, when
from typer.testing import CliRunner


# ---------------------------------------------------------------------------
# State carriers
# ---------------------------------------------------------------------------


@dataclass
class CLIResult:
    exit_code: int
    stdout: str


@dataclass
class World:
    """Shared mutable state for a single scenario."""

    root: Path
    pypi_releases: dict[str, dict[str, int]] = field(default_factory=dict)
    npm_releases: dict[str, dict[str, int]] = field(default_factory=dict)
    npm_installed: dict[str, str] = field(default_factory=dict)
    result: CLIResult | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> pendulum.DateTime:
    return pendulum.now("UTC")


def _iso(days_ago: int) -> str:
    return _now().subtract(days=days_ago).to_iso8601_string()


def _install_pypi_mocks(world: World) -> None:
    for package, releases in world.pypi_releases.items():
        respx.get(f"{PYPI_REGISTRY}/{package}/json").mock(
            return_value=httpx.Response(
                200,
                json={
                    "releases": {
                        version: [{"upload_time_iso_8601": _iso(age)}]
                        for version, age in releases.items()
                    }
                },
            )
        )


def _install_npm_mocks(world: World) -> None:
    for package, releases in world.npm_releases.items():
        respx.get(f"{NPM_REGISTRY}/{package}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "name": package,
                    "time": {version: _iso(age) for version, age in releases.items()},
                    "versions": {
                        version: {"name": package, "version": version, "dependencies": {}}
                        for version in releases
                    },
                },
            )
        )


def _fake_subprocess_for_npm(world: World):
    def runner_fn(cmd, **kw):
        argv = cmd if isinstance(cmd, list) else cmd.split()
        if "list" in argv:
            payload = {
                "dependencies": {
                    name: {"version": version}
                    for name, version in world.npm_installed.items()
                }
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    return runner_fn


def _fake_subprocess_for_pypi():
    def runner_fn(cmd, **kw):
        argv = cmd if isinstance(cmd, list) else cmd.split()
        return subprocess.CompletedProcess(argv, 0, "", "")

    return runner_fn


def _flush_project_to_disk(world: World, project_state: dict) -> None:
    deps_block = ", ".join(f'"{spec}"' for spec in project_state["deps"])
    (world.root / "pyproject.toml").write_text(
        '[project]\n'
        'name = "fixture"\n'
        'version = "0.1.0"\n'
        f"dependencies = [{deps_block}]\n"
    )

    lock_lines = ["version = 1\n"]
    for package, version in project_state["lock"]:
        lock_lines.append("[[package]]\n")
        lock_lines.append(f'name = "{package}"\n')
        lock_lines.append(f'version = "{version}"\n')
        lock_lines.append("\n")
    (world.root / "uv.lock").write_text("".join(lock_lines))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(root=tmp_path)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given("a fresh pypi project at the working directory", target_fixture="project_state")
def _fresh_project(world: World) -> dict:
    return {"deps": [], "lock": []}


@given(parsers.parse('the project depends on "{package}" pinned at "{version}"'))
def _add_pinned_dep(project_state: dict, package: str, version: str) -> None:
    project_state["deps"].append(f"{package}=={version}")
    project_state["lock"].append((package, version))


@given(parsers.parse('the project depends on "{package}" with spec "{spec}"'))
def _add_spec_dep(project_state: dict, package: str, spec: str) -> None:
    project_state["deps"].append(spec)


@given(parsers.parse('the lockfile resolves "{package}" to "{version}"'))
def _resolve_in_lock(project_state: dict, package: str, version: str) -> None:
    project_state["lock"].append((package, version))


@given(parsers.parse('pypi reports "{package} {version}" was published {age:d} days ago'))
def _record_pypi_release(world: World, package: str, version: str, age: int) -> None:
    world.pypi_releases.setdefault(package, {})[version] = age


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when(parsers.parse('I run "{command}"'))
def _run_cli(world: World, runner: CliRunner, project_state: dict, command: str) -> None:
    _flush_project_to_disk(world, project_state)
    args = command.split()
    assert args[0] == "chill-out", f"BDD only invokes the chill-out CLI, got: {command!r}"

    with respx.mock:
        _install_pypi_mocks(world)
        _install_npm_mocks(world)
        with patch(
            "chill_out.ecosystems.pypi.subprocess.run",
            side_effect=_fake_subprocess_for_pypi(),
        ):
            with patch(
                "chill_out.ecosystems.npm.subprocess.run",
                side_effect=_fake_subprocess_for_npm(world),
            ):
                result = runner.invoke(cli, [*args[1:], "--root", str(world.root)])
    world.result = CLIResult(exit_code=result.exit_code, stdout=result.stdout)


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then("the command exits cleanly")
def _exit_zero(world: World) -> None:
    assert world.result is not None
    assert world.result.exit_code == 0, f"unexpected exit {world.result.exit_code}\n{world.result.stdout}"


@then("the command exits with a violation")
def _exit_violation(world: World) -> None:
    assert world.result is not None
    assert world.result.exit_code == 2, f"expected exit 2, got {world.result.exit_code}\n{world.result.stdout}"


@then(parsers.parse('the output contains "{text}"'))
def _output_contains(world: World, text: str) -> None:
    assert world.result is not None
    assert text in world.result.stdout, f"missing {text!r} in:\n{world.result.stdout}"


@then(parsers.parse('the output mentions "{text}"'))
def _output_mentions(world: World, text: str) -> None:
    assert world.result is not None
    assert text in world.result.stdout, f"missing {text!r} in:\n{world.result.stdout}"


@then(parsers.parse('the output does not mention "{text}" in the violation table'))
def _output_omits_in_table(world: World, text: str) -> None:
    assert world.result is not None
    table_section = world.result.stdout.split("Strategy", 1)
    if len(table_section) > 1:
        body = table_section[1]
        assert text not in body, f"{text!r} unexpectedly appeared in the violation table:\n{body}"


@then(parsers.parse('the manifest contains "{text}"'))
def _manifest_contains(world: World, text: str) -> None:
    contents = (world.root / "pyproject.toml").read_text()
    assert text in contents, f"missing {text!r} in pyproject.toml:\n{contents}"

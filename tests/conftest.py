"""
Shared pytest fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pendulum
import pytest
from typer.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fixed_now() -> pendulum.DateTime:
    """Anchor 'now' for deterministic age calculations."""
    return pendulum.datetime(2026, 1, 1, tz="UTC")


@pytest.fixture
def npm_project(tmp_path: Path) -> Path:
    """A minimal npm project root with a package.json."""
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "fixture-app",
                "version": "1.0.0",
                "dependencies": {"left-pad": "^1.3.0"},
            },
            indent=2,
        )
    )
    return tmp_path


@pytest.fixture
def pypi_project(tmp_path: Path) -> Path:
    """A minimal Python project root with a pyproject.toml and uv.lock."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\n'
        'name = "fixture-app"\n'
        'version = "0.1.0"\n'
        'dependencies = ["requests==2.31.0", "click==8.1.7"]\n'
    )
    (tmp_path / "uv.lock").write_text(
        'version = 1\n'
        '\n'
        '[[package]]\n'
        'name = "requests"\n'
        'version = "2.31.0"\n'
        '\n'
        '[[package]]\n'
        'name = "click"\n'
        'version = "8.1.7"\n'
        '\n'
        '[[package]]\n'
        'name = "urllib3"\n'
        'version = "2.0.7"\n'
        'dependencies = []\n'
        '\n'
        '[[package]]\n'
        'name = "fixture-app"\n'
        'version = "0.1.0"\n'
        '[[package.dependencies]]\n'
        'name = "requests"\n'
        '\n'
        '[[package.dependencies]]\n'
        'name = "click"\n'
    )
    return tmp_path

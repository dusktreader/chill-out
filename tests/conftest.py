"""
Shared pytest fixtures.
"""

import json
from pathlib import Path

import pendulum
import pytest
import tomlkit
from chill_out.ecosystems import retry as _retry_module
from tenacity import wait_none
from typer.testing import CliRunner


@pytest.fixture(autouse=True)
def _instant_registry_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip backoff from the registry retry helper for the test run.

    Production uses exponential jitter with a few-second worst case, which
    would add real wall time to every retry-exercising test. The retry logic
    itself is what we want to verify; the timing is a separate concern.
    """
    # Tenacity attaches the runtime config to the wrapped callable as a
    # `retry` attribute, but the type stubs don't surface it. Reach for the
    # attribute via `getattr` to keep ty quiet.
    retrying = getattr(_retry_module.retried_get, "retry")
    monkeypatch.setattr(retrying, "wait", wait_none())


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
    pyproject = {
        "project": {
            "name": "fixture-app",
            "version": "0.1.0",
            "dependencies": ["requests==2.31.0", "click==8.1.7"],
        },
    }
    (tmp_path / "pyproject.toml").write_text(tomlkit.dumps(pyproject))

    lockfile = {
        "version": 1,
        "package": [
            {"name": "requests", "version": "2.31.0"},
            {"name": "click", "version": "8.1.7"},
            {"name": "urllib3", "version": "2.0.7", "dependencies": []},
            {
                "name": "fixture-app",
                "version": "0.1.0",
                "dependencies": [{"name": "requests"}, {"name": "click"}],
            },
        ],
    }
    (tmp_path / "uv.lock").write_text(tomlkit.dumps(lockfile))
    return tmp_path

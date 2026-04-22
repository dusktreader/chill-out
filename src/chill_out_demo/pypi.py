"""
Demos that exercise the full check pipeline against a Python project fixture.

Each demo creates a tiny pyproject.toml in a temp directory, points
`PypiRegistryClient` at a mock-backed httpx client, and runs the real
`check_async` orchestrator end-to-end.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import httpx
import pendulum
import respx

from chill_out import PypiEcosystem, check_async, plan_fixes
from chill_out.constants import EcosystemKind
from chill_out.ecosystems.pypi import PYPI_REGISTRY


def _iso(days_ago: int) -> str:
    return pendulum.now("UTC").subtract(days=days_ago).to_iso8601_string()


def _make_pypi_project(tmp: Path) -> None:
    (tmp / "pyproject.toml").write_text(
        '[project]\n'
        'name = "demo-app"\n'
        'version = "0.1.0"\n'
        'dependencies = ["fresh-pkg==2.0.0", "settled-pkg==1.0.0"]\n'
    )
    (tmp / "uv.lock").write_text(
        'version = 1\n'
        '[[package]]\nname = "fresh-pkg"\nversion = "2.0.0"\n\n'
        '[[package]]\nname = "settled-pkg"\nversion = "1.0.0"\n'
    )


def _seed_registry() -> None:
    respx.get(f"{PYPI_REGISTRY}/fresh-pkg/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "releases": {
                    "2.0.0": [{"upload_time_iso_8601": _iso(1)}],
                    "1.5.0": [{"upload_time_iso_8601": _iso(120)}],
                }
            },
        )
    )
    respx.get(f"{PYPI_REGISTRY}/settled-pkg/json").mock(
        return_value=httpx.Response(
            200, json={"releases": {"1.0.0": [{"upload_time_iso_8601": _iso(400)}]}}
        )
    )


@respx.mock
def demo_01_pypi_check() -> None:
    """
    Run a full check against a synthetic Python project.

    The project declares two dependencies — one published yesterday (in
    cooldown) and one published over a year ago. `check_async` returns a
    populated `CheckReport` that callers can render however they like.
    """
    _seed_registry()
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        _make_pypi_project(tmp)
        ecosystem = PypiEcosystem(tmp)
        report = asyncio.run(check_async(ecosystem))
    print(f"checked {len(report.checked)} package(s)")
    for v in report.violations:
        safe = v.safe_version.version if v.safe_version else "(none)"
        print(f"  ! {v.name}=={v.version} bump={v.bump.value} safe={safe}")


@respx.mock
def demo_02_pypi_plan_fixes() -> None:
    """
    Convert a check report into a list of `FixAction`s.

    `plan_fixes` deduplicates and chooses the smallest safe version when the
    same package appears more than once. The actions are printable and easy to
    feed back into `Ecosystem.apply_fixes`.
    """
    _seed_registry()
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        _make_pypi_project(tmp)
        report = asyncio.run(check_async(PypiEcosystem(tmp)))
    actions = plan_fixes(report)
    print(json.dumps([a.__dict__ for a in actions], indent=2, default=str))


@respx.mock
def demo_03_pypi_ecosystem_kind() -> None:
    """
    Each `InstalledPackage` carries its origin ecosystem.

    This is useful when programmatic callers mix npm and pypi reports — every
    record knows which registry it came from.
    """
    _seed_registry()
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        _make_pypi_project(tmp)
        report = asyncio.run(check_async(PypiEcosystem(tmp)))
    for pkg in report.checked:
        print(f"{pkg.name:15s} {pkg.version:8s} ecosystem={pkg.ecosystem.value}")
    assert all(p.ecosystem is EcosystemKind.PYPI for p in report.checked)

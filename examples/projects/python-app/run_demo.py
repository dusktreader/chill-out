#!/usr/bin/env python
"""
End-to-end demo of chill-out against a real-shaped Python project.

PyPI is mocked with `respx` and the `uv lock` subprocess call is stubbed,
so this demo runs offline and never modifies the on-disk fixture. Everything
else is the real chill-out pipeline.

Run with:

    uv run python examples/projects/python-app/run_demo.py

The script:

1. Loads the example project's config.
2. Runs a deep check against a mocked PyPI that places `httpx==0.27.0` and the
   transitive `anyio==4.3.0` inside the cooldown window.
3. Prints the violations and the fix plan. The plan exercises chill-out's
   principal-rollback path: the installed `fastapi==0.110.0` declares
   `anyio>=4.3,<5`, which doesn't admit the safe transitive `anyio==4.2.0`,
   so chill-out rolls fastapi back to `0.109.2` whose declared range does.
4. Applies the fixes to a copy of the project so the on-disk fixture stays
   pristine, and prints the resulting `pyproject.toml`.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pendulum
import respx
from chill_out import PypiEcosystem, check_async, plan_fixes_async
from chill_out.config import load_config
from chill_out.constants import EcosystemKind
from chill_out.ecosystems.pypi import PYPI_REGISTRY

PROJECT_ROOT = Path(__file__).parent
NOW = pendulum.datetime(2026, 1, 1, tz="UTC")


def _iso(days_ago: int) -> str:
    return NOW.subtract(days=days_ago).to_iso8601_string()


# ---------------------------------------------------------------------------
# Registry seeding
# ---------------------------------------------------------------------------
#
# Each entry mirrors a slice of PyPI's JSON API:
#   - `releases`: maps version -> publish age in days, used to build the
#     `releases` payload returned by GET /pypi/{name}/json
#   - `manifests`: maps version -> requires_dist list, returned by
#     GET /pypi/{name}/{version}/json under `info.requires_dist`

PACKAGES: dict[str, dict[str, Any]] = {
    "fastapi": {
        "releases": {
            "0.110.0": _iso(60),
            "0.109.2": _iso(120),
            "0.109.0": _iso(150),
        },
        "manifests": {
            # Installed fastapi declares a tight anyio range that excludes 4.2.x,
            # so a plain transitive override would violate the resolver. That
            # forces principal rollback to an older fastapi whose range admits
            # anyio==4.2.0.
            "0.110.0": ["anyio>=4.3,<5", "starlette>=0.36,<0.37"],
            "0.109.2": ["anyio>=3.7.1,<5", "starlette>=0.35,<0.37"],
            "0.109.0": ["anyio>=3.7.1,<5", "starlette>=0.35,<0.36"],
        },
    },
    "httpx": {
        "releases": {
            "0.27.0": _iso(2),
            "0.26.0": _iso(120),
            "0.25.2": _iso(200),
        },
        "manifests": {
            "0.27.0": ["httpcore"],
            "0.26.0": ["httpcore"],
            "0.25.2": ["httpcore"],
        },
    },
    "rich": {
        "releases": {"13.7.1": _iso(180)},
        "manifests": {"13.7.1": ["markdown-it-py>=2.2.0"]},
    },
    "pytest": {
        "releases": {"8.1.0": _iso(180)},
        "manifests": {"8.1.0": []},
    },
    "anyio": {
        "releases": {
            "4.3.0": _iso(2),
            "4.2.0": _iso(120),
            "4.0.0": _iso(300),
        },
        "manifests": {
            "4.3.0": ["sniffio>=1.1"],
            "4.2.0": ["sniffio>=1.1"],
        },
    },
    "starlette": {
        "releases": {"0.36.3": _iso(180)},
        "manifests": {"0.36.3": ["anyio>=3.4.0,<5"]},
    },
    "sniffio": {
        "releases": {"1.3.1": _iso(400)},
        "manifests": {"1.3.1": []},
    },
    "httpcore": {
        "releases": {"1.0.4": _iso(180)},
        "manifests": {"1.0.4": []},
    },
    "markdown-it-py": {
        "releases": {"3.0.0": _iso(800)},
        "manifests": {"3.0.0": []},
    },
}


def _seed_registry() -> None:
    """Wire respx mocks for the package index and per-version manifests."""
    for name, data in PACKAGES.items():
        releases_payload = {
            ver: [{"upload_time_iso_8601": iso}] for ver, iso in data["releases"].items()
        }
        respx.get(f"{PYPI_REGISTRY}/{name}/json").mock(
            return_value=httpx.Response(200, json={"releases": releases_payload}),
        )
        for ver, requires in data["manifests"].items():
            respx.get(f"{PYPI_REGISTRY}/{name}/{ver}/json").mock(
                return_value=httpx.Response(
                    200,
                    json={"info": {"requires_dist": list(requires)}},
                ),
            )


def _fake_subprocess_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """Stub `uv lock` only. Anything else is unexpected."""
    if cmd[:2] == ["uv", "lock"]:
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
    raise AssertionError(f"unexpected subprocess call in demo: {cmd!r}")


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


@respx.mock
def main() -> None:
    _seed_registry()
    config = load_config(PROJECT_ROOT, EcosystemKind.PYPI)

    ecosystem = PypiEcosystem(PROJECT_ROOT)
    report = asyncio.run(check_async(ecosystem, config=config, deep=True, now=NOW))
    actions = asyncio.run(plan_fixes_async(report, ecosystem, config=config, now=NOW))

    print("=" * 70)
    print(f"chill-out check: {PROJECT_ROOT.name}")
    print("=" * 70)
    print(f"checked {len(report.checked)} package(s)")
    if not report.violations:
        print("  no violations")
    for v in report.violations:
        safe = v.safe_version.version if v.safe_version else "(none)"
        kind = "transitive" if v.via else "direct"
        via = f" (via {v.via})" if v.via else ""
        print(f"  ! {kind:10s} {v.name}=={v.version}{via}")
        print(f"      release_type={v.release_type.value} age={v.age_days}d limit={v.limit_days}d safe={safe}")

    print()
    print("planned fix actions:")
    if not actions:
        print("  (none)")
    for a in actions:
        kind = "override " if a.is_override else "pin      "
        print(f"  {kind} {a.package} -> {a.version}")

    print()
    print("applying fixes to a temporary copy of the project ...")
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp) / "python-app-copy"
        shutil.copytree(PROJECT_ROOT, tmp)
        copy_eco = PypiEcosystem(tmp)
        with patch("chill_out.ecosystems.pypi.subprocess.run", side_effect=_fake_subprocess_run):
            applied = copy_eco.apply_fixes(actions)
        print("apply log:")
        for line in applied:
            print(f"  - {line}")
        patched = (tmp / "pyproject.toml").read_text()
        print()
        print("resulting pyproject.toml:")
        print(patched)


if __name__ == "__main__":
    main()

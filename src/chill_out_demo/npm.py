"""
Demos that exercise the full check pipeline against an npm project fixture.

These mock the npm registry with `respx` and stub the `npm list` subprocess
call so the demo never needs the real `npm` binary.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
import pendulum
import respx
from chill_out import NpmEcosystem, check_async, plan_fixes
from chill_out.ecosystems.npm import NPM_REGISTRY


def _iso(days_ago: int) -> str:
    return pendulum.now("UTC").subtract(days=days_ago).to_iso8601_string()


def _make_npm_project(tmp: Path) -> None:
    (tmp / "package.json").write_text(
        json.dumps(
            {
                "name": "demo-app",
                "version": "0.1.0",
                "dependencies": {"left-pad": "^1.0.0"},
            },
            indent=2,
        )
    )


def _seed_registry() -> None:
    respx.get(f"{NPM_REGISTRY}/left-pad").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "left-pad",
                "time": {"1.3.0": _iso(1), "1.2.0": _iso(120)},
            },
        )
    )


def _fake_npm_list() -> dict:
    return {"dependencies": {"left-pad": {"version": "1.3.0"}}}


@respx.mock
def demo_01_npm_check() -> None:
    """
    Run a check against a synthetic npm project.

    The npm registry is mocked with `respx` and the `npm list` subprocess is
    patched to return a known dependency graph, so the demo runs without ever
    invoking the real `npm` binary.
    """
    _seed_registry()
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        _make_npm_project(tmp)
        with patch.object(NpmEcosystem, "_npm_list", return_value=_fake_npm_list()):
            report = asyncio.run(check_async(NpmEcosystem(tmp)))
    print(f"checked {len(report.checked)} package(s)")
    for v in report.violations:
        safe = v.safe_version.version if v.safe_version else "(none)"
        print(f"  ! {v.name}@{v.version} release_type={v.release_type.value} safe={safe}")


@respx.mock
def demo_02_npm_plan_fixes() -> None:
    """
    Show the resulting fix actions for the npm violation.

    Every fix is a direct pin in `dependencies`. Transitive violations get
    promoted to direct entries so the resolver hoists them over whatever the
    principal asks for. Conflicts that can't be resolved by hoisting trigger
    a principal rollback handled by `plan_fixes_async`; the synchronous
    `plan_fixes` here just emits the direct pins.
    """
    _seed_registry()
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        _make_npm_project(tmp)
        with patch.object(NpmEcosystem, "_npm_list", return_value=_fake_npm_list()):
            report = asyncio.run(check_async(NpmEcosystem(tmp)))
    plan = plan_fixes(report)
    for action in plan.actions:
        print(f"pin {action.package}@{action.version}")
    for entry in plan.unfixable:
        print(f"unfixable {entry.violation.name}=={entry.violation.version}: {entry.reason}")

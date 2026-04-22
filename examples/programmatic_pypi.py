"""
Programmatic example: run a cooldown check on a Python project.

This script builds a tiny pyproject.toml in a temp directory, mocks the PyPI
JSON API with respx, and invokes the real `check_async` orchestrator against a
`PypiEcosystem`. It's the same code path used by the CLI's `check` command.

Run it with::

    uv run python examples/programmatic_pypi.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import httpx
import pendulum
import respx
from chill_out import PypiEcosystem, check_async, plan_fixes
from chill_out.ecosystems.pypi import PYPI_REGISTRY


def _iso(days_ago: int) -> str:
    return pendulum.now("UTC").subtract(days=days_ago).to_iso8601_string()


@respx.mock
def main() -> None:
    # Mock PyPI: a fresh 2.0.0 (in cooldown) and an older 1.5.0 (safe).
    respx.get(f"{PYPI_REGISTRY}/example-pkg/json").mock(
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

    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        (root / "pyproject.toml").write_text(
            '[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = ["example-pkg==2.0.0"]\n'
        )

        ecosystem = PypiEcosystem(root)
        report = asyncio.run(check_async(ecosystem))

    print(f"Checked {len(report.checked)} package(s); {len(report.violations)} violation(s).")
    for v in report.violations:
        print(f"  - {v.name}=={v.version}  release_type={v.release_type.value}  age={v.age_days}d  limit={v.limit_days}d")
        if v.safe_version:
            print(f"    safe rollback: {v.safe_version.version} ({v.safe_version.age_days}d old)")

    print("\nFix plan:")
    plan = plan_fixes(report)
    for action in plan.actions:
        print(f"  pin {action.package} -> {action.version}")
    for entry in plan.unfixable:
        print(f"  unfixable {entry.violation.name}=={entry.violation.version}: {entry.reason}")


if __name__ == "__main__":
    main()

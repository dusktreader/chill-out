"""
Build a `CooldownConfig` in code and pass it to `check_async`.

When the threshold values come from a database, a remote service, or just
hard-coded policy you can skip the file-based loader entirely and construct
the `CooldownConfig` yourself.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import httpx
import pendulum
import respx

from chill_out import BumpType, CooldownConfig, PypiEcosystem, check_async
from chill_out.ecosystems.pypi import PYPI_REGISTRY


def _iso(days_ago: int) -> str:
    return pendulum.now("UTC").subtract(days=days_ago).to_iso8601_string()


@respx.mock
def main() -> None:
    respx.get(f"{PYPI_REGISTRY}/example-pkg/json").mock(
        return_value=httpx.Response(
            200,
            json={"releases": {"1.0.0": [{"upload_time_iso_8601": _iso(3)}]}},
        )
    )

    # Strict policy: even patch releases need 14 days to settle.
    config = CooldownConfig(
        days={
            BumpType.MAJOR: 60,
            BumpType.MINOR: 30,
            BumpType.PATCH: 14,
            BumpType.DEFAULT: 14,
        }
    )

    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        (root / "pyproject.toml").write_text(
            '[project]\n'
            'name = "demo"\n'
            'version = "0.1.0"\n'
            'dependencies = ["example-pkg==1.0.0"]\n'
        )
        report = asyncio.run(check_async(PypiEcosystem(root), config=config))

    print(f"violations: {len(report.violations)}")
    for v in report.violations:
        print(f"  {v.name}=={v.version} (limit {v.limit_days}d, age {v.age_days}d)")


if __name__ == "__main__":
    main()

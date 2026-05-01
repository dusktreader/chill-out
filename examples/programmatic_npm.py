"""
Programmatic example: run a cooldown check on an npm project.

This is the npm counterpart to `programmatic_pypi.py`. It mocks both the npm
registry (with respx) and the `npm list` subprocess, so it runs without
needing the real `npm` binary on PATH.

Run it with::

    uv run python examples/programmatic_npm.py
"""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
import pendulum
import respx
from chill_out import NpmEcosystem, check_async
from chill_out.ecosystems.constants import NPM_REGISTRY


def _iso(days_ago: int) -> str:
    return pendulum.now("UTC").subtract(days=days_ago).to_iso8601_string()


@respx.mock
def main() -> None:
    respx.get(f"{NPM_REGISTRY}/left-pad").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "left-pad",
                "time": {"1.3.0": _iso(1), "1.2.0": _iso(120)},
            },
        )
    )

    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        (root / "package.json").write_text(
            json.dumps(
                {
                    "name": "demo",
                    "version": "0.1.0",
                    "dependencies": {"left-pad": "^1.0.0"},
                },
                indent=2,
            )
        )

        fake_npm_list = {"dependencies": {"left-pad": {"version": "1.3.0"}}}
        with patch.object(NpmEcosystem, "_npm_list", return_value=fake_npm_list):
            report = asyncio.run(check_async(NpmEcosystem(root)))

    for v in report.violations:
        safe = v.safe_version.version if v.safe_version else "(none)"
        print(f"{v.name}@{v.version} → safe={safe} (limit {v.limit_days}d, age {v.age_days}d)")


if __name__ == "__main__":
    main()

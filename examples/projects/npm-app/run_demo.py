#!/usr/bin/env python
"""
End-to-end demo of chill-out against a real-shaped npm project.

The npm registry is mocked with `respx` and the `npm list` and `npm install`
subprocess calls are stubbed, so the demo runs without ever needing the real
`npm` or `node` binaries on PATH. Everything else is the real chill-out
pipeline running against the real `package.json` and `package-lock.json` that
live alongside this script.

Run with:

    uv run python examples/projects/npm-app/run_demo.py

The script:

1. Loads the example project's config.
2. Runs a deep check against a mocked npm registry that places `chalk@5.4.0`
   and the transitive `lodash.merge@4.6.3` inside the cooldown window.
3. Prints the violations and the fix plan.
4. Applies the fixes to a *copy* of the project so the on-disk fixture stays
   pristine, and prints the resulting `package.json` so you can see exactly
   what `--fix` would have written.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pendulum
import respx
from chill_out import NpmEcosystem, check_async, plan_fixes_async
from chill_out.config import load_config
from chill_out.constants import EcosystemKind
from chill_out.ecosystems.npm import NPM_REGISTRY

PROJECT_ROOT = Path(__file__).parent
NOW = pendulum.datetime(2026, 1, 1, tz="UTC")


def _iso(days_ago: int) -> str:
    """ISO-8601 timestamp for a release published `days_ago` days before NOW."""
    return NOW.subtract(days=days_ago).to_iso8601_string()


# ---------------------------------------------------------------------------
# Registry seeding
# ---------------------------------------------------------------------------
#
# Each entry is a small slice of the public npm registry: a list of release
# timestamps plus the dependency manifest at one or more specific versions.
# The shape matches what the real registry returns for `GET /{name}` and
# `GET /{name}/{version}`.

PACKAGES: dict[str, dict[str, Any]] = {
    "chalk": {
        "releases": {
            "5.4.0": _iso(2),
            "5.3.0": _iso(120),
            "5.2.0": _iso(200),
        },
        "manifests": {
            "5.4.0": {"dependencies": {}},
            "5.3.0": {"dependencies": {}},
            "5.2.0": {"dependencies": {}},
        },
    },
    "express": {
        "releases": {"4.19.2": _iso(180)},
        "manifests": {"4.19.2": {"dependencies": {"accepts": "~1.3.8"}}},
    },
    "lodash": {
        "releases": {
            "4.17.21": _iso(900),
            "4.17.20": _iso(1200),
            "4.17.15": _iso(1800),
        },
        "manifests": {
            # The currently installed lodash declares its merge transitive
            # with a tight `~4.6.3` range so a rollback to 4.6.2 will *not*
            # satisfy it. That forces the principal-rollback path to consider
            # an older lodash whose declared range is permissive.
            "4.17.21": {"dependencies": {"lodash.merge": "~4.6.3"}},
            "4.17.20": {"dependencies": {"lodash.merge": "^4.6.0"}},
            "4.17.15": {"dependencies": {"lodash.merge": "^4.6.0"}},
        },
    },
    "lodash.merge": {
        "releases": {
            "4.6.3": _iso(2),
            "4.6.2": _iso(800),
        },
        "manifests": {
            "4.6.3": {"dependencies": {}},
            "4.6.2": {"dependencies": {}},
        },
    },
    "accepts": {
        "releases": {"1.3.8": _iso(700)},
        "manifests": {"1.3.8": {"dependencies": {}}},
    },
    "typescript": {
        "releases": {"5.4.5": _iso(150), "5.4.0": _iso(200)},
        "manifests": {"5.4.5": {"dependencies": {}}, "5.4.0": {"dependencies": {}}},
    },
}


def _seed_registry() -> None:
    """Wire respx mocks for every (package, version) combination above."""
    for name, data in PACKAGES.items():
        time_payload = {ver: iso for ver, iso in data["releases"].items()}
        respx.get(f"{NPM_REGISTRY}/{name}").mock(
            return_value=httpx.Response(200, json={"name": name, "time": time_payload}),
        )
        for ver, manifest in data["manifests"].items():
            respx.get(f"{NPM_REGISTRY}/{name}/{ver}").mock(
                return_value=httpx.Response(
                    200,
                    json={"name": name, "version": ver, **manifest},
                ),
            )


# ---------------------------------------------------------------------------
# `npm list` stub
# ---------------------------------------------------------------------------
#
# Mirrors what `npm list --all --json` would print for the package-lock.json
# in this directory: the four direct deps plus the lodash.merge transitive.

NPM_LIST_OUTPUT: dict[str, Any] = {
    "name": "chill-out-npm-example",
    "version": "1.0.0",
    "dependencies": {
        "chalk": {"version": "5.4.0"},
        "express": {
            "version": "4.19.2",
            "dependencies": {"accepts": {"version": "1.3.8"}},
        },
        "lodash": {
            "version": "4.17.21",
            "dependencies": {"lodash.merge": {"version": "4.6.3"}},
        },
        "typescript": {"version": "5.4.5"},
    },
}


def _fake_subprocess_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """Stub for `npm install` only. Other subprocess calls are passed through."""
    if cmd[:2] == ["npm", "install"]:
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
    raise AssertionError(f"unexpected subprocess call in demo: {cmd!r}")


# A tiny semver-range checker that covers the operators used in this fixture
# (`^x.y.z`, `~x.y.z`, exact match). The real `NpmEcosystem.range_satisfies`
# shells out to `node -e require('semver').satisfies(...)`, but the demo
# patches that out so it stays runnable without a populated node_modules.
def _fake_range_satisfies(self: NpmEcosystem, version: str, range_spec: str) -> bool:
    from packaging.version import Version

    v = Version(version)
    spec = range_spec.strip()
    if spec.startswith("^"):
        base = Version(spec[1:])
        return v >= base and v.major == base.major
    if spec.startswith("~"):
        base = Version(spec[1:])
        return v >= base and v.major == base.major and v.minor == base.minor
    return version == spec


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


@respx.mock
def main() -> None:
    _seed_registry()
    config = load_config(PROJECT_ROOT, EcosystemKind.NPM)

    # Run the check + plan against the real on-disk fixture.
    ecosystem = NpmEcosystem(PROJECT_ROOT)
    with (
        patch.object(NpmEcosystem, "_npm_list", return_value=NPM_LIST_OUTPUT),
        patch.object(NpmEcosystem, "range_satisfies", _fake_range_satisfies),
    ):
        report = asyncio.run(check_async(ecosystem, config=config, deep=True, now=NOW))
        actions = asyncio.run(
            plan_fixes_async(report, ecosystem, config=config, now=NOW),
        )

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
        print(f"  ! {kind:10s} {v.name}@{v.version}{via}")
        print(f"      release_type={v.release_type.value} age={v.age_days}d limit={v.limit_days}d safe={safe}")

    print()
    print("planned fix actions:")
    if not actions.actions:
        print("  (none)")
    for a in actions.actions:
        print(f"  pin {a.package} -> {a.version}")
    if actions.unfixable:
        print()
        print("unfixable violations:")
        for u in actions.unfixable:
            print(f"  ! {u.violation.name}=={u.violation.version}")
            print(f"      {u.reason}")

    # Apply the fixes against a copy so the fixture on disk stays pristine.
    print()
    print("applying fixes to a temporary copy of the project ...")
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp) / "npm-app-copy"
        shutil.copytree(PROJECT_ROOT, tmp)
        copy_eco = NpmEcosystem(tmp)
        with patch("chill_out.ecosystems.npm.subprocess.run", side_effect=_fake_subprocess_run):
            applied = copy_eco.apply_fixes(actions.actions)
        print("apply log:")
        for line in applied:
            print(f"  - {line}")
        patched = json.loads((tmp / "package.json").read_text())
        print()
        print("resulting package.json:")
        print(json.dumps(patched, indent=2))


if __name__ == "__main__":
    main()

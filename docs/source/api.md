# Programmatic API

Every CLI feature is also a regular Python function. If your build tooling is
already in Python, calling `chill-out` directly is faster, lets you hold onto
the structured report, and skips spawning a subprocess.


## The simplest call

```python
from pathlib import Path
from chill_out import check

report = check(Path.cwd())

if report.has_violations:
    for v in report.violations:
        print(f"{v.name}=={v.version} rel_type={v.release_type.value}")
```

`check` is the synchronous wrapper around `check_async`. It auto-detects the
ecosystem from the given root.


## Async with full control

```python
import asyncio
from pathlib import Path

from chill_out import PypiEcosystem, check_async, plan_fixes

async def main() -> None:
    eco = PypiEcosystem(Path.cwd())
    report = await check_async(eco, deep=True, fast=False)
    for action in plan_fixes(report):
        kind = "override" if action.is_override else "dependency"
        print(f"{kind}: {action.package} -> {action.version}")

asyncio.run(main())
```

`check_async` accepts an optional `httpx.AsyncClient` so you can share a
connection pool with the rest of your application.


## Planning fixes with principal rollback

`plan_fixes(report)` returns the simple set of overrides and pins. When a
transitive dep is in cooldown but its principal's declared range can't admit
the safe transitive, that override would either silently lie (npm) or break
the resolver (pip / uv). `plan_fixes_async` handles those cases by walking
older principal versions for one whose range *does* admit the safe
transitive, and emitting both a principal install and a transitive override.

```python
import asyncio
from pathlib import Path

import httpx
from chill_out import PypiEcosystem, check_async, plan_fixes_async

async def main() -> None:
    eco = PypiEcosystem(Path.cwd())
    async with httpx.AsyncClient() as http:
        report = await check_async(eco, http=http)
        actions = await plan_fixes_async(report, eco, http=http)
    for action in actions:
        kind = "override" if action.is_override else "install"
        print(f"{kind}: {action.package} -> {action.version}")

asyncio.run(main())
```

Sharing the `httpx.AsyncClient` between `check_async` and `plan_fixes_async`
means the registry cache built up during the check is reused during planning.


## Building a report manually

`CooldownConfig`, `release_type`, `is_within_cooldown`, and `find_safe_version`
are pure functions. Use them when your release data already lives somewhere
other than a public registry — an internal mirror, an SBOM, a cached database
row.

```python
import pendulum
from chill_out import (
    ReleaseType, CooldownConfig, PackageInfo, PackageRelease,
    find_safe_version, is_within_cooldown, release_type,
)

now = pendulum.now("UTC")
info = PackageInfo(
    name="example",
    releases={
        "2.0.0": PackageRelease("2.0.0", now.subtract(days=1)),
        "1.5.0": PackageRelease("1.5.0", now.subtract(days=60)),
    },
)
config = CooldownConfig(days={ReleaseType.MAJOR: 30, ReleaseType.DEFAULT: 5})

rel_type = release_type("2.0.0")
violating, age, limit = is_within_cooldown(info.published_at("2.0.0"), rel_type, config)
safe = find_safe_version("2.0.0", info, config)
```


## Catching errors

Every `chill-out`-raised error inherits from `ChillOutError`. The hierarchy:

```text
ChillOutError
├── ConfigError       — bad cooldown config
├── EcosystemError    — detection or manifest problem
├── RegistryError     — registry call failed
└── CooldownViolation — raised by tooling that wants to short-circuit on violation
```

Each subclass carries its own default `ExitCode`, which the CLI uses to choose
its exit status. Library code rarely needs that field, but it's there.


## Examples directory

The `examples/` folder ships short scripts demonstrating each entry point:

- `programmatic_pypi.py` — full PyPI check
- `programmatic_npm.py` — full npm check
- `custom_config.py` — passing a hand-built `CooldownConfig`
- `inspect_safe_versions.py` — pure helpers, no network
- `cli_check.sh` — shell-side equivalent of the simplest call

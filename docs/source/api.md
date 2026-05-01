# Programmatic API

Every CLI feature is also a regular Python function. If your build tooling is already in Python, calling `chill-out`
directly is faster, lets you hold onto the structured report, and skips spawning a subprocess.


## The simplest call

```python
from pathlib import Path
from chill_out import check

report = check(Path.cwd())

if report.has_violations:
    for v in report.violations:
        print(f"{v.name}=={v.version} rel_type={v.release_type.value}")
```

`check` is the synchronous wrapper around `check_async`. It auto-detects the ecosystem from the given root.


## Async with full control

```python
import asyncio
from pathlib import Path

from chill_out import PypiEcosystem, check_async, plan_fixes

async def main() -> None:
    eco = PypiEcosystem(Path.cwd())
    report = await check_async(eco, fast=False)
    plan = plan_fixes(report)
    for action in plan.actions:
        print(f"pin {action.package} -> {action.version}")
    for entry in plan.unfixable:
        print(f"unfixable {entry.violation.name}=={entry.violation.version}: {entry.reason}")

asyncio.run(main())
```

`check_async` accepts an optional `httpx.AsyncClient` so you can share a connection pool with the rest of your
application. It also accepts two callbacks for wiring up progress reporting without coupling to a particular UI library:

```python
report = await check_async(
    eco,
    on_start=lambda packages: print(f"checking {len(packages)} packages"),
    on_progress=lambda pkg: print(f"  done: {pkg.name}"),
)
```

`on_start` fires once with the full list of packages about to be checked. `on_progress` fires once per package after it
has been evaluated, including packages that were skipped because the registry returned no data. The CLI uses these to
drive a Rich progress bar; library callers can drive any UI that exposes "set total" and "advance" semantics.


## Choosing a fix style

`plan_fixes` accepts a `fix_style` keyword that controls the shape of each emitted pin. The default is `FixStyle.EXACT`,
which mirrors what the CLI does without `--fix-style`:

```python
from chill_out import FixStyle, plan_fixes

plan = plan_fixes(report, fix_style=FixStyle.COMPATIBLE)
for action in plan.actions:
    print(action.package, action.style, action.version)
```

`plan_fixes_async` reads the style from `config.fix_style` instead of taking it as a kwarg, since it already takes a
full `ChillOutConfig`. Pass a config with the field set:

```python
from chill_out import load_config

config = load_config(eco.root, eco.kind).model_copy(update={"fix_style": FixStyle.COMPATIBLE})
plan = await plan_fixes_async(report, eco, config=config, http=http)
```

In both forms, override-bound and principal-rollback actions stay exact regardless of the requested style. The `style`
field on each `FixAction` records what actually got written.


## Planning fixes with conflict-aware rollback

`plan_fixes(report)` returns a `FixPlan` with a flat list of direct pins and a list of `UnfixableViolation` entries.
Both lists may be empty.

For transitive violations the synchronous form just emits a direct pin and trusts the resolver to hoist it. That works
for npm, but pip and uv refuse to resolve a pin that falls outside the principal's declared range. The async form
`plan_fixes_async` handles those cases by walking older principal versions for one whose declared range _does_ admit the
safe transitive, and emitting both a principal rollback and the transitive pin. When no compatible older principal
exists, the violation lands in `FixPlan.unfixable` with a structured reason that lists the maintainer's options.

```python
import asyncio
from pathlib import Path

import httpx
from chill_out import PypiEcosystem, check_async, plan_fixes_async

async def main() -> None:
    eco = PypiEcosystem(Path.cwd())
    async with httpx.AsyncClient() as http:
        report = await check_async(eco, http=http)
        plan = await plan_fixes_async(report, eco, http=http)
    for action in plan.actions:
        print(f"pin {action.package} -> {action.version}")
    for entry in plan.unfixable:
        print(f"  ! {entry.violation.name}=={entry.violation.version}")
        print(f"    {entry.reason}")

asyncio.run(main())
```

Sharing the `httpx.AsyncClient` between `check_async` and `plan_fixes_async` means the registry cache built up during
the check is reused during planning.


## Building a report manually

The cooldown engine in `chill_out.cooldown` is a handful of pure functions sitting on top of `ChillOutConfig`. Reach
for them when your release data already lives somewhere other than a public registry: an internal mirror, an SBOM, a
cached database row. They're not re-exported from the top-level `chill_out` namespace because they only make sense
paired with a version parser, and the parser depends on which ecosystem you're modeling.

A parser is just a callable that turns a version string into a `ParsedVersion` (or `None` for inputs the ecosystem
can't classify). Each ecosystem ships one as a method on its class, so the easy path is to instantiate the ecosystem
you want and grab its `parse_version`:

```python
from pathlib import Path

import pendulum
from chill_out import ChillOutConfig, NpmEcosystem, PackageInfo, PackageRelease, ReleaseType
from chill_out.cooldown import find_safe_version, is_within_cooldown, release_type

# Use whichever ecosystem matches the version flavor of your data. The root
# argument doesn't have to point at a real project; the parser is pure.
parser = NpmEcosystem(root=Path(".")).parse_version

now = pendulum.now("UTC")
info = PackageInfo(
    name="example",
    releases={
        "2.0.0": PackageRelease("2.0.0", now.subtract(days=1)),
        "1.5.0": PackageRelease("1.5.0", now.subtract(days=60)),
    },
)
config = ChillOutConfig(cooldown_days={ReleaseType.MAJOR: 30, ReleaseType.DEFAULT: 5})

rel_type = release_type("2.0.0", parser)
violating, age, limit = is_within_cooldown(info.published_at("2.0.0"), rel_type, config)
safe = find_safe_version("2.0.0", info, config, parser)
```

For Python data, swap in `PypiEcosystem(...).parse_version`. The two parsers obey their own ecosystems' rules: npm's
parser is strict semver, while pypi's understands PEP 440 (epochs, post-releases, the works) so two-segment versions
like `idna 3.12` classify the way users actually expect.


## Catching errors

Every `chill-out`-raised error inherits from `ChillOutError`. The hierarchy:

```text
ChillOutError
├── ConfigError       — bad cooldown config
├── EcosystemError    — detection or manifest problem
├── RegistryError     — registry call failed
├── StateError        — state file corrupt, unreadable, or written under an unknown schema
└── CooldownViolation — raised by tooling that wants to short-circuit on violation
```

`StateError` has four concrete subclasses (`StateFileUnreadableError`, `StateFileCorruptError`,
`StateSchemaVersionError`, `StateValidationError`), each pointing at a different failure mode of `.chill-out-state.json`.
They all live in `chill_out.state` and re-export from the top-level `chill_out` namespace.

Each subclass carries its own default `ExitCode`, which the CLI uses to choose its exit status. Library code rarely
needs that field, but it's there.


## Examples directory

The `examples/` folder ships short scripts demonstrating each entry point:

- `programmatic_pypi.py` — full PyPI check
- `programmatic_npm.py` — full npm check
- `custom_config.py` — passing a hand-built `ChillOutConfig`
- `inspect_safe_versions.py` — pure helpers, no network
- `cli_check.sh` — shell-side equivalent of the simplest call


## Next stops

- [Reference](reference.md) for the full auto-generated API documentation
- [Configuration](configuration.md) for the schema behind `ChillOutConfig`
- [Examples](examples.md) for narrated walk-throughs of the scripts listed above
- [CLI](cli.md) for the command-line surface that wraps this same API

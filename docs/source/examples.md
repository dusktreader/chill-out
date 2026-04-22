# Worked examples

The two example projects under `examples/projects/` are complete enough to feel
like real codebases: a `package.json` with a lockfile, a `pyproject.toml` with
a `uv.lock`, a `.chill-out.yaml` config alongside, and a runnable demo script
that exercises the whole check + fix pipeline against them.

Both demos mock the registry so they run offline. The chill-out code path is
the real one: configuration loading, ecosystem detection, transitive
attribution, cooldown evaluation, conflict-aware principal rollback, and the
file edits that `--fix` would write are all the production functions, not
stand-ins.


## How `--fix` decides what to write

For every violation with a known safe version, chill-out tries the simplest
thing first: a direct pin in the project's primary manifest
(`dependencies` for npm, `project.dependencies` for pypi). Direct pins win
because the resolver hoists them above whatever the principal asks for.

For a transitive violation, the planner first checks whether the installed
principal's declared range admits the safe transitive. If it does, the direct
pin is enough. If not, chill-out looks for an older principal whose declared
range *does* admit the safe transitive, and emits two pins: a rollback of the
principal and the direct pin of the transitive. If no compatible older
principal exists the violation lands in `FixPlan.unfixable` with a structured
reason so the CLI can show actionable guidance instead of silently dropping
the violation.


## npm: a fresh direct + a fresh transitive

The fixture lives at `examples/projects/npm-app/`. Its `package.json` declares
`chalk`, `express`, `lodash`, and a dev dep on `typescript`. Both the lockfile
and the demo's mocked registry agree that:

- `chalk@5.4.0` was published two days ago. It is a direct dep, so chill-out
  proposes a direct pin to a safe older release.
- `lodash.merge@4.6.3` was published two days ago. It is pulled in by `lodash`,
  and the installed `lodash@4.17.21` declares its merge transitive with a
  range that does not admit `lodash.merge@4.6.2`. The planner walks back to
  `lodash@4.17.20`, whose declared range does admit `4.6.2`, and emits both
  the principal rollback and the transitive pin.

Run it from the repo root:

```bash
uv run python examples/projects/npm-app/run_demo.py
```

The output:

```text
======================================================================
chill-out check: npm-app
======================================================================
checked 6 package(s)
  ! direct     chalk@5.4.0
      release_type=minor age=2d limit=14d safe=5.3.0
  ! transitive lodash.merge@4.6.3 (via lodash)
      release_type=patch age=2d limit=7d safe=4.6.2

planned fix actions:
  pin chalk -> 5.3.0
  pin lodash -> 4.17.20
  pin lodash.merge -> 4.6.2

applying fixes to a temporary copy of the project ...
apply log:
  - pinned chalk -> 5.3.0
  - pinned lodash -> 4.17.20
  - pinned lodash.merge -> 4.6.2
  - ran: npm install

resulting package.json:
{
  "name": "chill-out-npm-example",
  "version": "1.0.0",
  ...
  "dependencies": {
    "chalk": "5.3.0",
    "express": "^4.19.2",
    "lodash": "4.17.20",
    "lodash.merge": "4.6.2"
  },
  "devDependencies": {
    "typescript": "^5.4.0"
  }
}
```

Three things to notice in the resulting manifest:

1. The fresh direct dep (`chalk`) became a hard pin in `dependencies`. The
   caret range was replaced with the exact safe version.
2. The fresh transitive (`lodash.merge`) was promoted to a direct entry in
   `dependencies`. The npm resolver hoists this entry over whatever the
   principal asks for.
3. The principal (`lodash`) was rolled back from `^4.17.21` to a hard pin at
   `4.17.20`, the most recent version whose declared range admits the safe
   transitive. This is the conflict-resolution step: without the rollback,
   the resolver would either fail or silently disagree about which
   `lodash.merge` to use.


## Python: principal rollback through a transitive

The fixture at `examples/projects/python-app/` declares `fastapi`, `httpx`,
`rich`, and a dev dep on `pytest`. The mocked PyPI plays the same trick:

- `httpx==0.27.0` was published two days ago and is a direct dep. It gets
  pinned to `0.26.0`.
- `anyio==4.3.0` was published two days ago and is pulled in by fastapi. The
  installed `fastapi==0.110.0` declares `anyio>=4.3,<5`, which excludes the
  safe `anyio==4.2.0`. Chill-out rolls fastapi back to `0.109.2`, whose
  declared `anyio>=3.7.1,<5` admits `4.2.0`, and pins anyio directly.

Without the rollback, `uv lock` would refuse to resolve a pinned
`anyio==4.2.0` against fastapi's declared `anyio>=4.3,<5`, and the manifest
edit would leave the project in a half-applied state.

Run it:

```bash
uv run python examples/projects/python-app/run_demo.py
```

The output:

```text
======================================================================
chill-out check: python-app
======================================================================
checked 9 package(s)
  ! transitive anyio==4.3.0 (via fastapi)
      release_type=minor age=2d limit=14d safe=4.2.0
  ! direct     httpx==0.27.0
      release_type=minor age=2d limit=14d safe=0.26.0

planned fix actions:
  pin fastapi -> 0.109.2
  pin anyio -> 4.2.0
  pin httpx -> 0.26.0

applying fixes to a temporary copy of the project ...
apply log:
  - pinned fastapi -> 0.109.2
  - added anyio==4.2.0 to project.dependencies
  - pinned httpx -> 0.26.0
  - ran: uv lock

resulting pyproject.toml:
[project]
name = "chill-out-python-example"
version = "0.1.0"
...
dependencies = [
    "fastapi==0.109.2",
    "httpx==0.26.0",
    "rich==13.7.1",
    "anyio==4.2.0",
]
```

The Python ecosystem doesn't have npm-style `overrides`, so transitive pins
land as direct entries in `project.dependencies`. After the edit, `uv lock`
runs and the lockfile reflects the rolled-back fastapi plus the pinned anyio.


## When the planner gives up

If a transitive conflict has no compatible older principal (for example, the
principal first declared the offending range several major versions back), the
direct pin would still get attempted but the planner records an
`UnfixableViolation`. The CLI surfaces these explicitly:

```text
1 violation(s) cannot be auto-fixed:
  - leftpad==2.0.0: safe version 1.5.0 conflicts with parent@2.0.0
    (declares leftpad>=2.0), and no older parent release that has cleared
    its own cooldown declares a range that admits leftpad==1.5.0.
    Options: downgrade parent manually, raise the safe target for leftpad,
    or wait out the cooldown.
```

The reason string lists the three concrete actions a maintainer can take.
Programmatic callers can iterate `FixPlan.unfixable` and surface the same
guidance in their own UIs.


## How to copy this for your own project

The runnable demos are an honest template for two common automation patterns:

- A pre-merge CI check that runs `chill-out check` and fails the build if any
  cooldown violation is fresh enough to need attention.
- A scheduled job that runs `chill-out check --fix` and opens a PR with the
  resulting manifest changes.

For real CI, drop the mocks and let chill-out talk to the real registry. The
fixture projects exist so you can read a known-good `package.json` and
`pyproject.toml` side-by-side without standing up a real npm install or
running `uv lock` yourself.

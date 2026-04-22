# Worked examples

The two example projects under `examples/projects/` are complete enough to feel
like real codebases: a `package.json` with a lockfile, a `pyproject.toml` with
a `uv.lock`, a `.chill-out.yaml` config alongside, and a runnable demo script
that exercises the whole check + fix pipeline against them.

Both demos mock the registry so they run offline. The chill-out code path is
the real one: configuration loading, ecosystem detection, transitive
attribution, cooldown evaluation, principal rollback, and the file edits that
`--fix` would write are all the production functions, not stand-ins.


## npm: a fresh direct + a fresh transitive

The fixture lives at `examples/projects/npm-app/`. Its `package.json` declares
`chalk`, `express`, `lodash`, and a dev dep on `typescript`. Both the lockfile
and the demo's mocked registry agree that:

- `chalk@5.4.0` was published two days ago. It is a direct dep, so chill-out
  will propose pinning it to a safe older release.
- `lodash.merge@4.6.3` was published two days ago. It is pulled in by `lodash`,
  and `lodash@4.17.21` declares its merge transitive with a tight `~4.6.3`
  range. That range does not admit `lodash.merge@4.6.2`, so a plain override
  would create an install that no version of the principal accepts. Chill-out's
  principal-rollback path notices, walks back to `lodash@4.17.20` whose
  declared range does admit `4.6.2`, and emits both the principal pin and the
  transitive override.

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
  dependency chalk -> 5.3.0
  dependency lodash -> 4.17.20
  override  lodash.merge -> 4.6.2

applying fixes to a temporary copy of the project ...
apply log:
  - dependency chalk -> 5.3.0
  - dependency lodash -> 4.17.20
  - override lodash.merge -> 4.6.2
  - ran: npm install

resulting package.json:
{
  "name": "chill-out-npm-example",
  "version": "1.0.0",
  ...
  "dependencies": {
    "chalk": "5.3.0",
    "express": "^4.19.2",
    "lodash": "4.17.20"
  },
  "devDependencies": {
    "typescript": "^5.4.0"
  },
  "overrides": {
    "lodash.merge": "4.6.2"
  }
}
```

Three things to notice in the resulting manifest:

1. The fresh direct dep (`chalk`) became a hard pin in `dependencies`. The
   caret range was replaced with the exact safe version.
2. The fresh transitive (`lodash.merge`) became an entry in npm's `overrides`
   block, so npm will resolve every reference to that name to the safe version
   regardless of what the parent declares.
3. The principal (`lodash`) was rolled back from `^4.17.21` to a hard pin at
   `4.17.20`, the most recent version whose declared range admits the safe
   transitive. Without that rollback, npm would complain that the override
   conflicts with the principal's declared range.


## Python: principal rollback through a transitive

The fixture at `examples/projects/python-app/` declares `fastapi`, `httpx`,
`rich`, and a dev dep on `pytest`. The mocked PyPI plays the same trick:

- `httpx==0.27.0` was published two days ago and is a direct dep. It gets
  pinned to `0.26.0`.
- `anyio==4.3.0` was published two days ago and is pulled in by fastapi. The
  installed `fastapi==0.110.0` declares `anyio>=4.3,<5`, which excludes the
  safe `anyio==4.2.0`. Chill-out rolls fastapi back to `0.109.2`, whose
  declared `anyio>=3.7.1,<5` admits `4.2.0`, and emits the override.

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
  pin       fastapi -> 0.109.2
  override  anyio -> 4.2.0
  pin       httpx -> 0.26.0

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

The Python ecosystem doesn't have npm-style `overrides`. To pin a transitive
that was previously implicit, chill-out promotes it to a direct entry in
`project.dependencies`. After the edit, `uv lock` runs and the lockfile reflects
the rolled-back fastapi plus the pinned anyio.


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

# Implementation Notes

This document captures what was built when porting the original
`check-dep-cooldown.py` script into the `chill-out` library, and the design
choices made along the way.


## Goal

The starting point was a 939-line single-file script
(`assess-item-analyzer/infra/scripts/check-dep-cooldown.py`) that read
Dependabot's `cooldown` configuration, walked an npm project's installed
dependencies, and reported any package versions that were younger than the
configured grace period. The aim of this project was to keep the spirit of
that script while turning it into:

- A real installable Python library with a public API
- A CLI tool that works in npm and Python projects
- A test suite, documentation, demos, and runnable examples


## Architecture

The code is organized into small, single-purpose modules under `src/chill_out/`:

| Module                    | Responsibility                                              |
|---------------------------|-------------------------------------------------------------|
| `constants.py`            | Enums (`ExitCode`, `ReleaseType`, `EcosystemKind`) and defaults |
| `exceptions.py`           | `ChillOutError` hierarchy + `@handle_errors` CLI decorator  |
| `models.py`               | Plain pydantic-free dataclasses for the domain types        |
| `config.py`               | Layered configuration loader                                |
| `cooldown.py`             | Pure functions that decide whether a release is in cooldown |
| `runner.py`               | Orchestrates a check across one project                     |
| `reporting.py`            | Renders results to the terminal with `rich`                 |
| `cli/main.py`             | Typer CLI: `check`, `show-config`, `version`                |
| `ecosystems/base.py`      | `Ecosystem` and `RegistryClient` abstract base classes      |
| `ecosystems/npm.py`       | npm backend (registry client + lockfile reader + fixer)     |
| `ecosystems/pypi.py`      | PyPI backend (registry client + uv.lock reader + fixer)     |
| `ecosystems/registry.py`  | `detect_ecosystem` and `get_ecosystem` lookup helpers       |

The split between "ecosystem" and "core" exists so that adding a new package
manager (Cargo, Go modules, RubyGems) is a matter of writing one new
`Ecosystem` subclass. The core runner does not know what an npm or a PyPI is.


## Design choices

### Pluggable ecosystem backends

The original script hard-coded npm. This implementation defines an
`Ecosystem` ABC with three required methods: `detect`, `iter_installed`, and
`apply_fixes`. The runner picks a backend by either an explicit
`--ecosystem` flag or by autodetection based on which manifest files exist
in the project root.

### Layered configuration

Configuration is resolved by merging four sources, in decreasing precedence:

1. `.chill-out.yaml` at the project root
2. The project's primary manifest: `[tool.chill-out]` in `pyproject.toml`
   for Python projects, or the top-level `"chill-out"` key in `package.json`
   for npm projects (the two slots sit at the same priority tier)
3. The `cooldown` block from `.github/dependabot.yml`
4. Built-in defaults (7 / 14 / 30 / 60 days)

The Dependabot tier exists so that an existing repo with cooldown windows
already configured for Dependabot picks them up without duplicating the
settings.

### Fix mode

For PyPI projects, `--fix` edits `pyproject.toml` in place using `tomlkit`
(which preserves comments and formatting), then runs `uv lock` to refresh
the lockfile. For npm, it adds `overrides` entries to `package.json` for
transitive deps and runs `npm install` to refresh `package-lock.json`.

### Principal rollback

When a transitive dependency is in cooldown, the simple fix is to pin the
transitive to an older safe version via an override. That works as long as
the principal package's declared range still admits the safe version. When
it doesn't, the override either silently lies (npm) or breaks the lock
resolution (pip / uv).

The fix planner handles this in `plan_fixes_async`:

1. Look up the principal's installed version in the report.
2. Fetch its manifest from the registry.
3. If the declared range for the transitive already accepts the safe
   version, emit just the override.
4. Otherwise, walk older principal versions (out of cooldown, prereleases
   skipped) and pick the newest one whose declared range *does* admit the
   safe transitive. Emit two actions: install that older principal, and
   override the transitive.

The range check is delegated to the ecosystem via `Ecosystem.range_satisfies`.
The npm implementation shells out to `node -e "require('semver').satisfies(...)"`,
matching the original script. The PyPI implementation uses
`packaging.SpecifierSet`. Both fall back permissively when the inputs can't
be parsed, mirroring the original's "assume compatible if we can't tell"
behavior.

When no compatible older principal exists, the planner skips the violation
with a warning rather than emitting a doomed action.

### Registry caching

`CachingRegistryClient` wraps any `RegistryClient` with a per-process,
in-memory dedupe cache for both `fetch_package` and `fetch_version_manifest`.
In-flight tasks are also tracked, so concurrent callers asking for the same
key share a single network round-trip. The runner wraps every client in this
cache by default. There's no disk persistence: a fresh process always hits
the registry once per (package, version), trading a tiny bit of network for
zero cache-invalidation pain.

### Documentation tooling

Docs are built with [`zensical`](https://github.com/squidfunk/zensical), the
successor to `mkdocs-material`. To keep `chill-out` itself free of doc-only
dependencies, the docs site is a separate nested `uv` project at
`docs/pyproject.toml` with `package = false` and an editable path source
back to the main package. The build command is:

```bash
uv run --project docs zensical build --config-file=docs/mkdocs.yaml
```


## Testing

There are 102 tests split across `tests/unit/` and `tests/integration/`,
running at 92% coverage (the gate is set at 85%):

- Unit tests cover each module in isolation, mocking out `httpx` calls with
  `respx` and patching subprocess calls in the fixers.
- Integration tests in `tests/integration/test_cli_integration.py` invoke
  the real Typer app end-to-end against fixture projects with both
  ecosystems mocked.

Run the suite with `make qa/test`. Run lint, typecheck, and tests with
`make qa/full`.


## Layout

```
chill-out/
|-- src/
|   |-- chill_out/         # The library
|   `-- chill_out_demo/    # Demo entry points
|-- tests/
|   |-- unit/
|   `-- integration/
|-- examples/
|   |-- *.py *.sh           # Per-API micro-templates
|   `-- projects/           # Full example projects with manifests + lockfiles
|       |-- npm-app/        # package.json + package-lock.json + .chill-out.yaml
|       `-- python-app/     # pyproject.toml + uv.lock + .chill-out.yaml
|-- docs/                  # Nested uv project for the zensical site
|   |-- pyproject.toml
|   |-- mkdocs.yaml
|   `-- source/
|-- pyproject.toml
|-- Makefile
`-- README.md
```


## What is not done

A few things from the original script were intentionally deferred:

- **Monorepo / workspace support.** v1 deliberately treats each project as a
  flat single-root layout. The npm backend reads only the root `package.json`
  and ignores nested ones; the pypi backend reads only the root
  `pyproject.toml` and ignores `[tool.uv.workspace]` members. For a monorepo,
  run `chill-out` from each sub-project's directory. A workspace-aware mode
  (per-member detection, dispatch with `npm install --workspace=` and
  `uv sync --package=`) is a candidate for a future release.
- **Disk-backed registry cache.** The in-memory cache is per-process. CI
  runs that invoke `chill-out` repeatedly will refetch each time. A simple
  TTL cache under `platformdirs.user_cache_dir()` would close that gap.

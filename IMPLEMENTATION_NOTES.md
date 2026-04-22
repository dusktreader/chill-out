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
| `constants.py`            | Enums (`ExitCode`, `BumpType`, `EcosystemKind`) and defaults |
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
2. `[tool.chill-out]` in `pyproject.toml`
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

The original script had a more elaborate "principal rollback" strategy
where, if pinning a transitive dep created an unsatisfiable constraint, it
would walk up the dependency tree and roll back the principal package
instead. That is **not** yet implemented here. For now, the fixer pins
overrides directly and surfaces an error if the override is incompatible.

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
|-- examples/              # Runnable scripts demonstrating the API
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

- **Principal rollback for incompatible transitive overrides.** When pinning
  a transitive dep to an older version conflicts with a principal's
  declared range, the original script would search for an older principal
  version whose range admits the safe transitive. This implementation
  raises an error in that case instead.
- **Caching of registry responses.** Each run hits the registry fresh.
- **Parallel registry fetches.** The async backbone is in place but the
  runner currently fetches packages sequentially. This is fine for small
  projects and easy to switch on later.

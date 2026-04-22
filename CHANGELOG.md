# Change Log

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/)
and this project adheres to [Semantic Versioning](http://semver.org/).

## v0.1.0 - 2026-04-22

First public release. The package starts life as a port of an internal
single-file script and lands as a fully-shaped library with a CLI, a
programmatic API, two real ecosystem backends, and a docs site.

### Added
- `chill-out check` walks installed dependencies, asks the registry when each version was published, and reports anything still inside its cooldown window.
- pypi backend: reads `pyproject.toml` and `uv.lock`, queries the JSON API, runs `uv lock` to apply fixes.
- npm backend: reads `package.json` and `package-lock.json`, queries the npm registry, runs `npm install` to apply fixes. Detects npm workspaces and routes shared transitive fixes through `overrides`.
- Per release-type cooldown thresholds (major / minor / patch / default), configurable in days.
- Resolved configuration cascades through `.chill-out.yaml`, `[tool.chill-out]` in `pyproject.toml`, the `"chill-out"` key in `package.json`, the matching `dependabot.yml` cooldown block, and a built-in default.
- Dependency-group filtering. Defaults to `main` only so dev tooling and optional extras don't trip the cooldown check unless explicitly opted in.
- `--fix` rewrites the manifest with safe pins. Two styles: `exact` (the default, hard pin) and `compatible` (`>={existing},<{M+1}.0.0` on PyPI, `^x.y.z` on npm). The compatible style preserves any existing `>=` lower bound rather than collapsing it onto the safe version.
- Conflict-aware principal rollback. When a transitive violation can't be fixed by hoisting a direct pin (because the principal's declared range excludes the safe transitive), the planner walks older principal versions for one whose range admits the safe transitive and emits a paired rollback. Both legs of the rollback render exact regardless of `fix_style`, so the resolver can't drift back into the conflict.
- Override-style fallback for npm transitive conflicts that don't have a clean rollback target.
- `UnfixableViolation` records carry a structured reason listing the maintainer's options, so callers can surface real guidance instead of silently dropping a violation.
- Re-check by default after `--fix` to confirm the fix actually cleared every violation. `--no-recheck` skips that second pass.
- `--deep` walks transitive dependencies (slower, more thorough); `--fast` skips the safe-version lookup (faster, pass/fail only). The two compose for everything except `--fix`, which needs the safe-version data.
- On-disk cache for registry responses, keyed by `(package, version)` so repeated checks within a short window don't re-hit the network.
- Dependency-tree rendering for transitive violations, showing the principal at the root and the violating leaf at the bottom of the chain.
- Progress bar driven from the `on_start` / `on_progress` callbacks that `check_async` now exposes (library callers can drive any UI).
- Programmatic API: `check`, `check_async`, `plan_fixes`, `plan_fixes_async`, plus all the supporting models and helpers exported from `chill_out`.
- `chill-out show-config` prints the resolved cooldown thresholds and dependency-group selection.
- Stable exit codes (`0` clear, `2` violation, `3` config error, `4` ecosystem error, `5` registry error, `99` internal).
- Docs site at `https://dusktreader.github.io/chill-out` covering quickstart, configuration, ecosystems, CLI, programmatic API, GitHub Actions recipes, and a comparison page against Renovate, Dependabot, and pinning tools.
- Two complete worked-example projects under `examples/projects/` (one npm, one pypi) plus a `chill-out-demo` CLI shipped under the `[demo]` extra. Both demos mock the registry so they run offline.
- `py.typed` shipped in the wheel: the library is fully type-annotated.

### Notes

I dogfood `chill-out` on its own dependencies through `make qa/full`, which
runs `chill-out check` against the project itself. The current `rich`
requirement was already capped during development by running
`chill-out check --fix --fix-style compatible` on the repo (the resulting
`rich>=14.0,<15.0.0` is in this release).

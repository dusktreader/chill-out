# Change Log

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/) and this project adheres to
[Semantic Versioning](http://semver.org/).

## v0.1.0 - 2026-05-01

First public release. The package starts life as a port of an internal single-file script and lands as a fully-shaped
library with a CLI, a programmatic API, two real ecosystem backends, and a docs site.

### Added

- `chill-out check` walks installed dependencies, asks the registry when each version was published, and reports
  anything still inside its cooldown window.
- `chill-out fix` rewrites the manifest with safe pins. Two styles: `exact` (the default, hard pin) and `compatible`
  (`>={existing},<{M+1}.0.0` on PyPI, `^x.y.z` on npm). The compatible style preserves any existing `>=` lower bound
  rather than collapsing it onto the safe version.
- `chill-out reset` subcommand. By default it rolls back every pin chill-out previously wrote into your manifests,
  then deletes `.chill-out-state.json`. Pass `--no-rollback` to keep the pins in place and only forget about them, or
  `--dry-run` to preview the rollback without touching disk. Useful when the state file is corrupt, when you've
  decided to stop using chill-out, or when you want the next `fix` run to start from a clean slate.
- `chill-out audit` subcommand. Read-only sweep that takes every entry in `.chill-out-state.json`, asks the registry
  whether the avoided release has cleared its cooldown window or been pulled outright, and buckets each pin into stale
  (cooldown's done, pin can be retired), yanked (release is gone, pin can be retired with extra confidence), fresh
  (still within cooldown, pin doing its job), or unknown (registry couldn't classify, review manually). Exits `0` when
  every pin is fresh, exits `7` (`STATE_STALE`) when any pin is stale or yanked. Pairs with `chill-out fix --cleanup`
  for the actual retirement.
- `chill-out show-config` prints the resolved cooldown thresholds and dependency-group selection.
- pypi backend: reads `pyproject.toml` and `uv.lock`, queries the JSON API, runs `uv lock` to apply fixes.
- npm backend: reads `package.json` and `package-lock.json`, queries the npm registry, runs `npm install` to apply
  fixes. Detects npm workspaces and routes shared transitive fixes through `overrides`.
- Yank detection in both registry backends. The pypi backend collapses per-artifact `yanked` flags into a per-release
  decision (a release counts as yanked when every artifact is yanked, matching pip and uv). The npm backend marks any
  version absent from the registry response's `versions` map as yanked. The cooldown planner skips yanked candidates
  when picking a safe version, so chill-out won't pin you to a release the maintainer pulled.
- Per release-type cooldown thresholds (major / minor / patch / default), configurable in days.
- Resolved configuration cascades through `.chill-out.yaml`, `[tool.chill-out]` in `pyproject.toml`, the `"chill-out"`
  key in `package.json`, the matching `dependabot.yml` cooldown block, and a built-in default.
- Dependency-group filtering. Defaults to `main` only so dev tooling and optional extras don't trip the cooldown check
  unless explicitly opted in.
- Conflict-aware principal rollback. When a transitive violation can't be fixed by hoisting a direct pin (because the
  principal's declared range excludes the safe transitive), the planner walks older principal versions for one whose
  range admits the safe transitive and emits a paired rollback. Both legs of the rollback render exact regardless of
  `fix_style`, so the resolver can't drift back into the conflict.
- Override-style fallback for npm transitive conflicts that don't have a clean rollback target.
- `UnfixableViolation` records carry a structured reason listing the maintainer's options, so callers can surface real
  guidance instead of silently dropping a violation.
- Re-check by default after `chill-out fix` to confirm the fix actually cleared every violation. `--no-recheck` skips
  that second pass.
- `--fast` flag on `check` skips the safe-version lookup for a faster pass/fail signal.
- State files validate against a strict Pydantic schema with an explicit `schema_version`. Drift, unknown fields,
  bad types, or unsupported versions all halt with a typed error pointing at the offending field instead of silently
  pretending the file is empty.
- On-disk cache for registry responses, keyed by `(package, version)` so repeated checks within a short window don't
  re-hit the network.
- Dependency-tree rendering for transitive violations, showing the principal at the root and the violating leaf at the
  bottom of the chain.
- Progress bar driven from the `on_start` / `on_progress` callbacks that `check_async` exposes (library callers can
  drive any UI).
- Programmatic API: `check`, `check_async`, `plan_fixes`, `plan_fixes_async`, plus all the supporting models and helpers
  exported from `chill_out`.
- Stable exit codes (`0` clear, `2` violation, `3` config error, `4` ecosystem error, `5` registry error,
  `6` state error, `7` state stale, `99` internal).
- Docs site at `https://dusktreader.github.io/chill-out` covering quickstart, configuration, ecosystems, CLI,
  programmatic API, GitHub Actions recipes, and a comparison page against Renovate, Dependabot, and pinning tools.
- Case study docs page walking a small project through ninety days of dependency churn with chill-out in the loop.
  Covers the initial check, the first dependabot batch, a yanked-release scenario, transitive conflict resolution, and
  a major-version cooldown wait.
- Two complete worked-example projects under `examples/projects/` (one npm, one pypi) plus a `chill-out-demo` CLI
  shipped under the `[demo]` extra. Both demos mock the registry so they run offline.
- `py.typed` shipped in the wheel: the library is fully type-annotated.

### Notes

I dogfood `chill-out` on its own dependencies through `make qa/full`, which runs `chill-out check` against the project
itself. The current `rich` requirement was already capped during development by running
`chill-out fix --fix-style compatible` on the repo (the resulting `rich>=14.0,<15.0.0` is in this release).

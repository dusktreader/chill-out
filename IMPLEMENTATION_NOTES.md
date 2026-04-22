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
the lockfile. For npm, it pins each violating dep as a direct entry in
`dependencies` (transitive pins ride along as direct entries; the resolver
hoists them above the principal's declared range) and runs `npm install`
to refresh `package-lock.json`. This matches the upstream
`check-dep-cooldown.py` script, which dropped its `overrides` write path
after running into npm's workspace-hoisting unreliability.

### Conflict-aware principal rollback

When a transitive dependency is in cooldown, the simple fix is a direct pin
of the safe transitive in the project's primary manifest. That works as long
as the principal's declared range admits the safe version. When it doesn't,
npm silently disagrees about which copy to load and pip / uv refuse to
resolve at all.

The fix planner handles this in `plan_fixes_async` and returns a structured
`FixPlan(actions, unfixable)`:

1. **Direct violation:** emit a direct pin. Done.
2. **Transitive, principal range admits the safe version:** emit a direct
   pin and trust the resolver to hoist it. Done.
3. **Transitive, principal range conflicts:** walk older principal versions
   (out of cooldown, prereleases skipped) and pick the newest one whose
   declared range admits the safe transitive. Emit two actions: a pin of
   the older principal, and a direct pin of the transitive.
4. **No compatible older principal:** record the violation in
   `FixPlan.unfixable` with a structured reason listing the maintainer's
   options (downgrade the principal manually, raise the safe target, or
   wait out the cooldown).

The range check is delegated to the ecosystem via `Ecosystem.range_satisfies`.
The npm implementation shells out to `node -e "require('semver').satisfies(...)"`,
matching the original script. The PyPI implementation uses
`packaging.SpecifierSet`. Both fall back permissively when the inputs can't
be parsed, mirroring the original's "assume compatible if we can't tell"
behavior.

The upstream npm-only script dropped principal rollback because npm tolerates
the conflict (the resolver doesn't error out on a hoisted pin that disagrees
with a parent's declared range). We kept rollback because pypi doesn't
tolerate it: `uv lock` will refuse to resolve a pinned `anyio==4.2.0` against
fastapi's declared `anyio>=4.3,<5`, and the manifest edit would leave the
project in a half-applied state. Keeping rollback as the conflict-resolution
fallback gives both ecosystems the same shape: try the simple thing, fall
back to the principled thing only when the simple thing won't resolve.

### npm lockfile resolution

The npm backend builds a reverse-dep graph from `package-lock.json` so that
deep mode can attribute each transitive to a principal via BFS. The lookup
order matters because workspace members frequently have no lockfile of their
own:

1. `<root>/package-lock.json` — the standard location.
2. `<root>/node_modules/.package-lock.json` — npm writes one of these every
   time it installs, even when the project doesn't ship a top-level lockfile.
3. The same two paths walking up the directory tree to the filesystem root,
   so a workspace member can borrow its workspace root's lockfile.

If nothing turns up, the backend logs a warning and proceeds with an empty
graph; deep mode still enumerates packages but every transitive ends up
without a `via_chain`.

The lockfile entry keys can take nested forms like
`node_modules/foo/node_modules/bar` whenever the resolver couldn't hoist a
transitive to the top. The parent-name extraction splits on the *last*
`node_modules/` so `bar` (not `foo/node_modules/bar`) shows up as the
requirer for whatever `bar` itself depends on. The graph also reads from
`optionalDependencies` in addition to `dependencies` and `peerDependencies`
so optional transitives still get attributed correctly.

### npm workspace-member descent

Even in non-deep mode, running chill-out from inside a workspace member is
common, and `npm list` always walks up to the workspace root before reporting.
That means the local project's directly-declared deps don't show up at the top
of the npm-list tree; they appear nested one level deeper, under a
`file:`-resolved entry that represents the workspace member itself.

The direct-mode loader handles this by descending one level into any
`file:`-resolved top-level entry and matching that node's `dependencies`
against the local `package.json`'s declared names. Deeper nesting (workspaces
inside workspaces) is treated as transitive territory and stays out of direct
mode. The workspace member's own entry is never reported as a package.

### npm deep-mode workspace scoping and per-(name, version) reporting

Deep mode has the same workspace-from-member problem as direct mode. When
`npm list --all --json` runs from a workspace member it walks up to the
workspace root and reports every member's tree, so a naive walk would surface
sibling members' transitives and try to fix them through the wrong
`package.json`.

The deep loader reads the running project's own `package.json` to learn its
declared name, then locates that name as a top-level dependency in the
npm-list output and uses just that subtree as the collection root. When
`self.root` already equals the workspace root the scoping is skipped.

The collector dedupes by `(name, version)` rather than by name. npm routinely
installs the same package at multiple distinct versions — one hoisted to the
shallowest `node_modules`, others nested under specific parents — and each
copy actually loads at runtime for whichever code requires it. Reporting only
one (whether the shallowest or the first-seen) hides real installations that
can violate cooldown independently.

The npm-list tree gives us the exact ancestor path that pulled in each copy,
so `via_chain` for transitives is read straight from the walk position rather
than reconstructed from the lockfile's reverse-dep graph. The earlier
`_build_required_by` helper is no longer used.

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


## Override fallback for stuck npm pins

A direct pin in `dependencies` doesn't always dislodge a violating version.
The most common case is npm's hoisting plus a sticky lockfile: a
workspace-member's pin lands in `<member>/node_modules/<pkg>`, but a
different consumer in the workspace pulled in the older version, the
resolver hoisted that older copy to the workspace root's `node_modules`,
and the lockfile pins both. The pin is technically present but the
violating copy still lives at the root.

The CLI now handles this with a two-strategy ladder. After `--fix` and the
verification recheck:

1. If a violation our fix targeted is still present, the runner gathers
   the corresponding `FixAction`s as "stuck pins" and asks the ecosystem
   to apply them via its override mechanism. For npm, this writes to the
   `overrides` field of the workspace root's `package.json` (located via
   `_find_lockfile`, not `self.root` -- overrides only apply tree-wide
   when set at the workspace root) and re-runs `npm install` from that
   directory.
2. After the second install, the runner re-checks again. Anything still
   in violation gets called out as "survived both direct pin and
   overrides" with an explanation pointing to manual remediation
   (delete the lockfile and reinstall, or pin the violating ancestor
   directly).

The override fallback is opt-in per ecosystem. `Ecosystem.apply_override_fixes`
defaults to returning `None`, and the CLI treats that as "not supported"
and falls back to the existing manual-remediation message. pypi doesn't
implement it (uv's `[tool.uv].override-dependencies` is conceptually
similar but interacts with workspaces differently and isn't worth wiring
up without a concrete failure case to verify against).

Live verification on the `assess-item-analyzer` workspace exposed npm's
deeper pathology: even with `overrides` set on the workspace root,
`npm install` writes the override entry but doesn't always rewrite
existing lockfile pins for hoisted copies. The package count drops
(indicating dedupe ran), but the offending entry can persist. The
runner's surviving-violation message exists specifically to flag this
class so the user knows when the automated ladder has been exhausted
and a `rm package-lock.json && npm install` is the next move.


## Walk every ancestor for transitive conflict checking

`plan_fixes_async` previously only checked the *principal*'s declared
range when deciding whether a transitive direct-pin would conflict. For
a deep chain like `principal -> middle -> child`, the principal's
manifest typically doesn't even mention `child`, so the check always
came back "no conflict" and we'd pin directly even when the middle
layer had a range that excluded the safe version.

The fix walks every entry in `via_chain` (immediate parent first,
principal last) and asks each ancestor's installed manifest whether it
declares a range for the violating package. The first conflicting
range we find triggers the principal-rollback flow. The principal stays
the rollback target (it's the only ancestor we can edit through the
project's own manifest), but the conflict-detection now sees the actual
constraint structure.


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

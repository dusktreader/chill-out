# Configuration

`chill-out` resolves its configuration from a single source. The supported sources are checked in priority order, and
the first one that exists wins. Anything below it is ignored entirely.

| Priority | Source                                                                     | Used for                                       |
| -------- | -------------------------------------------------------------------------- | ---------------------------------------------- |
| 1        | `.chill-out.{yaml,yml,toml,json}`                                          | Project-wide override; checked in to the repo. |
| 1        | `[tool.chill-out]` in `pyproject.toml`, or `"chill-out"` in `package.json` | Reuses the project's primary manifest.         |
| 2        | `.github/dependabot.yml` (matching ecosystem)                              | Fallback when no chill-out-native config exists. |
| --       | Built-in defaults                                                          | Fill in any fields the chosen source doesn't set. |

The chill-out-native sources (the dedicated file and the project-manifest blocks) all sit at the same priority. Pick
exactly one. If two or more are present at the same time, `chill-out` raises a configuration error rather than guessing
which one wins. The dedicated config file additionally accepts any of four extensions (`.chill-out.yaml`,
`.chill-out.yml`, `.chill-out.toml`, `.chill-out.json`); only one of those four may exist at a time too. Examples in
this guide use yaml; the same key structure works in toml or json.

`dependabot.yml` is a strict fallback. It's read only when no chill-out-native source exists at all, on the theory that
projects already using dependabot's cooldown should get something sensible without writing a second config file.

Whichever source is chosen, fields it doesn't specify fall back to the built-in defaults. For `cooldown`, that means
missing release types are filled in per-key from the defaults below: setting only `major: 60` still leaves you with the
default `minor`, `patch`, and `default` thresholds.


## Threshold values

Every source supplies the same four keys, in days:

| Key       | Default | Meaning                                     |
| --------- | ------- | ------------------------------------------- |
| `major`   | `30`    | Cooldown for major releases (`X.0.0`).      |
| `minor`   | `10`    | Cooldown for minor releases (`X.Y.0`).      |
| `patch`   | `7`     | Cooldown for patch releases (`X.Y.Z`).      |
| `default` | `5`     | Used when the release type can't be parsed. |

Release type is decided by parsing the version with semver. Pre-release versions are skipped during the safe-version
search.


## Included dependency groups

By default `chill-out` checks only the project's main runtime dependencies. Dev tooling, optional extras, and peer
dependencies stay out of the cooldown check unless you opt them in explicitly. The reasoning: a dev-only test runner
that ships a fresh release on Monday rarely warrants blocking your CI on Tuesday, but a runtime dependency that does so
is exactly the supply chain risk `chill-out` is built to mitigate.

The list is configured under the top-level `include_groups` key, valid in all source types. It accepts any combination
of these semantic group names:

| Name       | npm equivalent         | pypi equivalent                                                                         |
| ---------- | ---------------------- | --------------------------------------------------------------------------------------- |
| `main`     | `dependencies`         | `[project.dependencies]`                                                                |
| `dev`      | `devDependencies`      | `[dependency-groups.dev]` and `[project.optional-dependencies.dev]`                     |
| `optional` | `optionalDependencies` | every other `[project.optional-dependencies.*]` extra and `[dependency-groups.*]` group |
| `peer`     | `peerDependencies`     | (unused; PyPI has no equivalent)                                                        |

The default is `["main"]`. Set `include_groups: []` to check nothing (useful for temporarily disabling the check without
removing the config).

A package declared in more than one section accumulates every matching group; transitive dependencies inherit the union
of the groups of every top-level dependency that pulls them in. A transitive reachable through both `main` and `dev` is
included whenever either group is in `include_groups`.


## Fix style

When `chill-out fix` rewrites a manifest, the `fix_style` setting controls the shape of the resulting requirement. Two styles
are supported:

| Value        | PyPI rendering               | npm rendering | Future updates allowed                          |
| ------------ | ---------------------------- | ------------- | ----------------------------------------------- |
| `exact`      | `pkg==1.2.3`                 | `1.2.3`       | None; the version is pinned exactly.            |
| `compatible` | `pkg>={existing},<{M+1}.0.0` | `^1.2.3`      | Any non-major release that satisfies the range. |

The default is `exact`, which preserves the historical behavior: every fix produces a single concrete version. Pick
`compatible` if you'd rather let your resolver pick up patch and minor updates automatically while still capping the
next major behind another `chill-out` review.

For PyPI, `compatible` style preserves any existing `>=` lower bound rather than collapsing it onto the safe version. A
requirement like `rich>=14.0` violated by `rich 15.0.0` becomes `rich>=14.0,<15.0.0`, not `rich>=14.3.4,<15.0.0`. The
original lower bound only gets bumped if it sits above the safe version, in which case `chill-out` falls back to the
safe version as the floor.

Two cases always render exact regardless of the configured style, because both exist specifically to dodge a known-bad
version:

- **Overrides.** A version listed in the `overrides` config block is, by definition, a version you want pinned and
  nothing else.
- **Principal rollbacks.** When a transitive violation is resolved by rolling back a top-level dependency, both the
  principal pin and its paired transitive pin are written exactly so a range can't drift the resolver back into the
  original conflict.

`fix_style` is supported in every config source. It also has a CLI flag (`--fix-style`) that takes priority over the
resolved config:

```yaml
# .chill-out.yaml
fix_style: compatible
```

```toml
# pyproject.toml
[tool.chill-out]
fix_style = "compatible"
```

```json
{
  "chill-out": {
    "fix_style": "compatible"
  }
}
```

```bash
chill-out fix --fix-style compatible
```


## Examples

The examples below set `include_groups` to non-default values to show how to opt extra dependency groups in. If the key
is omitted entirely, chill-out scans only the `main` group.


### Dedicated YAML

The simplest source. Drop a file at the project root:

```yaml
# .chill-out.yaml
cooldown:
  major: 60
  minor: 14
  patch: 7
  default: 7
include_groups:
  - main
  - dev
```


### `pyproject.toml`

If you'd rather not add another config file, add a `[tool.chill-out]` table:

```toml
[tool.chill-out]
include_groups = ["main", "dev"]

[tool.chill-out.cooldown]
major = 60
minor = 14
patch = 7
default = 7
```


### `package.json`

The same idea works for npm projects: add a top-level `"chill-out"` key to `package.json`. The `cooldown` sub-key is
optional, mirroring the yaml and pyproject shapes:

```json
{
  "name": "my-app",
  "version": "1.0.0",
  "chill-out": {
    "include_groups": ["main", "peer"],
    "cooldown": {
      "major": 60,
      "minor": 14,
      "patch": 7,
      "default": 7
    }
  }
}
```

### Dependabot reuse

If you already have cooldown windows in `.github/dependabot.yml`, `chill-out` reads them automatically. The original
`dependabot.yml` keys are accepted as aliases so you can copy-paste:

```yaml
updates:
  - package-ecosystem: pip
    directory: "/"
    schedule:
      interval: weekly
    cooldown:
      semver-major-days: 30
      semver-minor-days: 10
      semver-patch-days: 7
      default-days: 5
```

`chill-out` filters by `package-ecosystem`: npm entries feed npm checks, pip entries feed Python checks. Dependabot
doesn't have a concept of dependency group filtering, so this source only ever supplies cooldown thresholds.


## Inspecting the resolved config

When you're not sure which source won, ask:

```bash
chill-out show-config
```

The output is the same threshold table and group list that `check` prints at the top of its report.


## Next stops

- [Ecosystems](ecosystems.md) for how each package manager's groups map onto `include_groups`
- [CLI](cli.md) for the flags that override these settings on a single run
- [GitHub Actions](github-actions.md) for wiring this configuration into CI
- [Programmatic API](api.md) for building a `ChillOutConfig` directly in code

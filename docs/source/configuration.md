# Configuration

`chill-out` resolves its cooldown thresholds from up to four sources, layered
in priority order. Each source can supply a partial mapping; missing keys
cascade down through the remaining sources until they hit the built-in
defaults.

| Priority | Source                                | Used for                                         |
|----------|---------------------------------------|--------------------------------------------------|
| 1        | `.chill-out.yaml` (or `.chill-out.yml`) | Project-wide override; checked in to the repo. |
| 2        | `[tool.chill-out.cooldown]` in `pyproject.toml` | Same role, but reuses an existing TOML file. |
| 3        | `.github/dependabot.yml` (matching ecosystem) | Reuses your Dependabot policy.            |
| 4        | Built-in defaults                       | Sensible fallback if nothing else applies.       |


## Threshold values

Every source supplies the same four keys, in days:

| Key       | Default | Meaning                                   |
|-----------|---------|-------------------------------------------|
| `major`   | `30`    | Cooldown for major releases (`X.0.0`).    |
| `minor`   | `10`    | Cooldown for minor releases (`X.Y.0`).    |
| `patch`   | `7`     | Cooldown for patch releases (`X.Y.Z`).    |
| `default` | `5`     | Used when the release type can't be parsed.  |

Release type is decided by parsing the version with semver. Pre-release versions
are skipped during the safe-version search.


## Examples


### Dedicated YAML

The simplest source. Drop a file at the project root:

```yaml
# .chill-out.yaml
cooldown:
  major: 60
  minor: 14
  patch: 7
  default: 7
```


### `pyproject.toml`

If you'd rather not add another config file, add a `[tool.chill-out.cooldown]`
table:

```toml
[tool.chill-out.cooldown]
major = 60
minor = 14
patch = 7
default = 7
```


### Dependabot reuse

If you already have cooldown windows in `.github/dependabot.yml`, `chill-out`
reads them automatically. The original `dependabot.yml` keys are accepted as
aliases so you can copy-paste:

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

`chill-out` filters by `package-ecosystem`: npm entries feed npm checks, pip
entries feed Python checks.


## Inspecting the resolved config

When you're not sure which source won, ask:

```bash
chill-out show-config
```

The output is the same threshold table that `check` prints at the top of its
report.

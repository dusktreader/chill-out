# Configuration

`chill-out` resolves its cooldown thresholds from up to four sources, layered
in priority order. Each source can supply a partial mapping; missing keys
cascade down through the remaining sources until they hit the built-in
defaults.

| Priority | Source                                | Used for                                         |
|----------|---------------------------------------|--------------------------------------------------|
| 1        | `.chill-out.yaml` (or `.chill-out.yml`) | Project-wide override; checked in to the repo. |
| 2        | `[tool.chill-out.cooldown]` in `pyproject.toml`, or `"chill-out"` in `package.json` | Reuses the project's primary manifest. |
| 3        | `.github/dependabot.yml` (matching ecosystem) | Reuses your Dependabot policy.            |
| 4        | Built-in defaults                       | Sensible fallback if nothing else applies.       |

The "primary manifest" tier picks whichever file the project already has. A
Python project supplies the table in `pyproject.toml`; an npm project supplies
it in `package.json`. The two slots sit at the same priority, so a project
that somehow ships both will see the npm key win on a tie (`package.json` is
loaded last among the manifest sources).


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


### `package.json`

The same idea works for npm projects: add a top-level `"chill-out"` key to
`package.json`. The `cooldown` sub-key is optional, mirroring the yaml and
pyproject shapes:

```json
{
  "name": "my-app",
  "version": "1.0.0",
  "chill-out": {
    "cooldown": {
      "major": 60,
      "minor": 14,
      "patch": 7,
      "default": 7
    }
  }
}
```

A flat map under `"chill-out"` is also accepted, in case you find the nested
key noisy for one-off configs.


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

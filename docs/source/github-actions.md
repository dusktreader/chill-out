# GitHub Actions

![chill-out-action](images/chill-action-600.png)

`chill-out` is built to run in CI. The check is read-only against your lockfile,
exits with a stable code, and prints a self-contained report.

The fastest way to wire it up is [`chill-out-action`](https://github.com/dusktreader/chill-out-action),
a composite action that handles setup, runs the CLI, and (optionally) opens a fix PR for you.
If you need more control than the action exposes, the [manual setup](#manual-setup) section
covers calling the CLI directly.


## chill-out-action

### Check on every pull request

Block a merge if any dependency is too fresh to trust:

```yaml
# .github/workflows/cooldown.yml
name: Cooldown check

on:
  pull_request:
    branches: [main]

jobs:
  chill-out:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: dusktreader/chill-out-action@v1
```

That's the whole workflow. The action auto-detects your ecosystem, installs the CLI
via `uvx`, and exits non-zero on any cooldown violation.

Pass `fast: true` if you only need pass/fail on PRs and want to skip the safe-version
lookup (saves a registry round-trip per violating package):

```yaml
- uses: dusktreader/chill-out-action@v1
  with:
    fast: true
```


### Scheduled fix PR

Run on a schedule, pin any violations automatically, and open a PR:

```yaml
# .github/workflows/cooldown-fix.yml
name: Cooldown fix

on:
  schedule:
    - cron: "0 9 * * 1"  # every Monday morning
  workflow_dispatch:

jobs:
  chill-out:
    runs-on: ubuntu-latest
    permissions:
      contents: write
      pull-requests: write
    steps:
      - uses: actions/checkout@v4
      - uses: dusktreader/chill-out-action@v1
        with:
          command: fix
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

When violations are found, the action commits the pinned lockfile to a
`fix/chill-out-<timestamp>` branch and opens a PR against the default branch.
If a fix PR is already open, it reuses that branch instead of opening a duplicate.

See the [`chill-out-action` README](https://github.com/dusktreader/chill-out-action)
for the full input/output reference.


## Picking when to run

The right trigger depends on how much friction you want and how quickly you need to know.
Each pattern below works with either `chill-out-action` or the manual setup.

The examples shown here don't conflict. A fast PR gate, a nightly audit, and an on-push
sanity check can coexist in separate workflow files.


### On every pull request

Catches a bad release the moment a contributor tries to bump it, before the merge.
Adds a few seconds of registry round-trips to every PR.

```yaml
on:
  pull_request:
    branches: [main]
```


### On every push to `main`

Same job, different trigger. You only learn about a bad release after it's landed,
but it's cheaper if you already gate merges on a green main.

```yaml
on:
  push:
    branches: [main]
```


### On a schedule

Best fit for projects whose dependencies you don't touch often. A weekly or daily
run notices when an already-installed dependency trips a fresh release downstream.
Pair it with `command: fix` to get automatic remediation PRs.

```yaml
on:
  schedule:
    - cron: "0 9 * * 1"  # every Monday morning
  workflow_dispatch:
```

----

## Manual setup

If you need control the action doesn't expose, call the CLI directly:

```yaml
jobs:
  chill-out:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uvx chill-out check
```

The manual approach is also useful when you want to compose chill-out with other
steps that inspect its output, like opening an issue when a violation is found:

```yaml
- name: Run cooldown check
  id: chill
  run: uvx chill-out check > chill-out-report.txt
  continue-on-error: true

- name: Open issue on violation
  if: steps.chill.outcome == 'failure'
  env:
    GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
  run: |
    gh issue create \
      --title "Cooldown violation detected" \
      --body-file chill-out-report.txt \
      --label "dependencies"
```

Capturing the report to a file keeps the issue body grounded in the actual run.


## Warn-only mode

To make the cooldown check visible without blocking unrelated PRs, put it in its
own job with `continue-on-error: true`:

```yaml
jobs:
  test:
    # ... your existing test matrix
  chill-out:
    runs-on: ubuntu-latest
    continue-on-error: true
    steps:
      - uses: actions/checkout@v4
      - uses: dusktreader/chill-out-action@v1
```

Drop `continue-on-error` once you trust the signal.


## Next stops

- [CLI](cli.md) for every flag the workflow examples pass to `chill-out`
- [Configuration](configuration.md) for the config files the workflows read at runtime
- [Examples](examples.md) for end-to-end recipes built around the same commands
- [Comparison](comparison.md) for how chill-out slots in alongside Dependabot or Renovate

# GitHub Actions

`chill-out` is built to run in CI. The check is read-only against your manifest, exits with a stable code, and prints a
self-contained report, which is everything a CI job needs.

This page collects a few recipes, ranging from "drop this in and you're done" to "I want a separate scheduled job that
opens an issue when something trips."


## The minimal recipe

The smallest workflow that catches uncooled dependencies on every pull request:

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
      - uses: astral-sh/setup-uv@v5
      - run: uv tool install chill-out
      - run: chill-out check --quiet
```

`uv tool install chill-out` puts the CLI on `PATH` without polluting the project's environment. `--quiet` drops the
threshold table from the top of the report so the log stays focused on the violations.

If a violation appears, the step exits with code `2` and the report is in the log. No fix is attempted, no manifest is
written, no commit is created.


## Picking when to run

A few patterns, ordered by how aggressive they are:

**On every pull request.** What the minimal recipe does. Catches a bad release the moment a contributor tries to bump
it, before the merge. Adds a few seconds of registry round-trips to every PR.

**On every push to `main`.** Same job, swap the trigger. Cheaper if you already gate merges on a green main, but you
only learn about a bad release after it's landed.

**On a schedule.** Best fit for projects whose dependencies you don't touch often. A daily cron run notices when an
already-installed dependency suddenly trips a fresh release downstream:

```yaml
on:
  schedule:
    - cron: "0 13 * * *" # 13:00 UTC every day
  workflow_dispatch:
```

A scheduled run paired with an issue-opening step is the classic "alert me when my supply chain shifts" pattern. Since
`chill-out` always audits the full lockfile (principals and transitives together), the scheduled job is the same
`chill-out check` invocation as the PR gate. The difference is the trigger, not the flags.

**All three.** The combinations don't conflict; you can have a fast PR gate, a scheduled nightly audit, and an on-push
sanity check coexisting in different workflow files.


## Speed knobs in CI

`chill-out check` makes one registry call per checked package, plus one more per violation when it computes a safe
rollback. For most projects that's a couple of seconds end-to-end. `--fast` skips the safe-version lookup, so PR gates
that only need pass/fail can save one extra registry round trip per violating package; the report still names the
violating package, it just doesn't suggest a rollback target. A typical split is `--fast` on PRs and no `--fast` on the
nightly cron where you want the rollback suggestions in the issue body.


## Failing the build, but separately

A common ask: "I want CI to know when chill-out is unhappy, but I don't want a fresh upstream release to fail unrelated
PRs." The answer is to put the cooldown check in its own job (or its own workflow), so its red cross is visible without
blocking the main test matrix:

```yaml
jobs:
  test:
    # ... your existing test matrix
  chill-out:
    runs-on: ubuntu-latest
    continue-on-error: true # warn-only
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv tool install chill-out
      - run: chill-out check
```

`continue-on-error: true` lets the job report failure on its summary without marking the workflow as failed. Drop it
once you trust the signal.


## Opening an issue when something trips

Pair the check with `peter-evans/create-issue-from-file` or a bare `gh` call to surface violations as tracked work:

```yaml
- name: Run cooldown check
  id: chill
  run: chill-out check > chill-out-report.txt
  continue-on-error: true

- name: Open an issue if a violation was found
  if: steps.chill.outcome == 'failure'
  env:
    GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
  run: |
    gh issue create \
      --title "Cooldown violation detected" \
      --body-file chill-out-report.txt \
      --label "dependencies"
```

Capturing the report to a file keeps the issue body grounded in the actual run rather than a hand-written summary that
drifts.


## Scheduled cleanup pull requests

Once you've been running `chill-out fix` for a while, your `.chill-out-state.json` accumulates pins for releases that
were too fresh at the time. Most of those releases will eventually clear cooldown, and at that point the pin is just
holding you back from upgrading. `chill-out fix --cleanup` (the default) handles this on demand, but a scheduled job
that opens a PR when there's cleanup to do means you don't have to remember.

The trick is the split between `chill-out audit` (read-only, exits `7` when there's cleanup waiting) and `chill-out fix`
(does the actual cleanup, exits `0` when it's done). `audit` is the trigger; `fix` is the action.

```yaml
# .github/workflows/cooldown-cleanup.yml
name: Cooldown cleanup PR

on:
  schedule:
    - cron: "0 13 * * 1" # 13:00 UTC every Monday
  workflow_dispatch:

permissions:
  contents: write
  pull-requests: write

jobs:
  cleanup:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv tool install chill-out

      - name: See if any pins are ready to retire
        id: audit
        run: chill-out audit
        continue-on-error: true

      - name: Run cleanup when audit flagged something
        if: steps.audit.outcome == 'failure'
        run: chill-out fix

      - name: Open a PR with the cleanup
        if: steps.audit.outcome == 'failure'
        uses: peter-evans/create-pull-request@v6
        with:
          branch: chill-out/cleanup
          title: "chill-out: retire pins that have cleared cooldown"
          commit-message: "chill-out: retire pins that have cleared cooldown"
          body: |
            Scheduled cleanup PR opened by the `chill-out audit` workflow.
            Every pin retired here was confirmed by the registry to have either
            cleared its cooldown window or been yanked outright. Review the
            manifest diff and merge when CI is green.
          labels: dependencies, chill-out
```

A few things worth knowing:

- `chill-out audit` exits `7` only when at least one pin is stale or yanked, which trips `continue-on-error` and lets
  the next two steps gate on `outcome == 'failure'`. Pure-fresh runs exit `0` and the job is a no-op.
- `chill-out fix` runs the cooldown check first. Anything currently inside its window stays pinned; only the cleared
  pins get retired. The PR diff stays small and predictable.
- The branch name is fixed (`chill-out/cleanup`), so re-running while a PR is already open updates the existing branch
  instead of opening a second one.
- `permissions:` has to grant `contents: write` and `pull-requests: write` for `create-pull-request` to push and open.
- `audit` exit code `7` is distinct from `check`'s `2`. CI can tell "your installed dependencies violate cooldown" apart
  from "your state file has cleanup work waiting" without parsing the report.

If you want `chill-out` to also fix any fresh violations the cleanup uncovered, the `chill-out fix` step already does
that: cleanup runs first, then a fresh check, then any new pins. The audit-then-fix split keeps the workflow honest
about why the PR is being opened.


## A real-world example

`chill-out` runs `chill-out check` against its own dependencies in its CI pipeline. The check is part of the `qa/full`
make target, so the same command runs locally and in GitHub Actions. The wired-up workflow lives at
[`.github/workflows/main.yml`](https://github.com/dusktreader/chill-out/blob/main/.github/workflows/main.yml) and the
make target is in the [Makefile](https://github.com/dusktreader/chill-out/blob/main/Makefile).


## Next stops

- [CLI](cli.md) for every flag the workflow examples pass to `chill-out check` and `chill-out fix`
- [Configuration](configuration.md) for the config files the workflows read at runtime
- [Examples](examples.md) for end-to-end recipes built around the same commands
- [Comparison](comparison.md) for how chill-out slots in alongside Dependabot or Renovate

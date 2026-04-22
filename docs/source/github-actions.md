# GitHub Actions

`chill-out` is built to run in CI. The check is read-only against your
manifest, exits with a stable code, and prints a self-contained report,
which is everything a CI job needs.

This page collects a few recipes, ranging from "drop this in and you're
done" to "I want a separate scheduled job that opens an issue when
something trips."


## The minimal recipe

The smallest workflow that catches uncooled dependencies on every pull
request:

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

`uv tool install chill-out` puts the CLI on `PATH` without polluting
the project's environment. `--quiet` drops the threshold table from
the top of the report so the log stays focused on the violations.

If a violation appears, the step exits with code `2` and the report
is in the log. No fix is attempted, no manifest is written, no commit
is created.


## Picking when to run

A few patterns, ordered by how aggressive they are:

**On every pull request.** What the minimal recipe does. Catches a bad
release the moment a contributor tries to bump it, before the merge.
Adds a few seconds of registry round-trips to every PR.

**On every push to `main`.** Same job, swap the trigger. Cheaper if you
already gate merges on a green main, but you only learn about a bad
release after it's landed.

**On a schedule.** Best fit for projects whose dependencies you don't
touch often. A daily cron run notices when an already-installed
dependency suddenly trips a fresh release downstream:

```yaml
on:
  schedule:
    - cron: "0 13 * * *"  # 13:00 UTC every day
  workflow_dispatch:
```

A scheduled run paired with `--deep` and an issue-opening step is the
classic "alert me when my supply chain shifts" pattern.

**All three.** The combinations don't conflict; you can have a fast PR
gate, a scheduled deep audit, and an on-push sanity check coexisting
in different workflow files.


## Speed knobs in CI

`chill-out check` makes one registry call per checked package, plus one
more per violation when it computes a safe rollback. For most projects
that's a couple of seconds end-to-end. Two flags help when you want it
faster or more thorough:

- `--fast` skips the safe-version lookup. Use it on PR gates where you
  only need pass/fail; the report still names the violating package, it
  just doesn't suggest a rollback target.
- `--deep` walks transitive dependencies too. Use it on scheduled runs
  where the extra coverage is worth the extra time.

The two flags compose. A typical split is `--fast` on PRs and
`--deep` (no `--fast`) on the nightly cron.


## Failing the build, but separately

A common ask: "I want CI to know when chill-out is unhappy, but I don't
want a fresh upstream release to fail unrelated PRs." The answer is to
put the cooldown check in its own job (or its own workflow), so its red
cross is visible without blocking the main test matrix:

```yaml
jobs:
  test:
    # ... your existing test matrix
  chill-out:
    runs-on: ubuntu-latest
    continue-on-error: true   # warn-only
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv tool install chill-out
      - run: chill-out check
```

`continue-on-error: true` lets the job report failure on its summary
without marking the workflow as failed. Drop it once you trust the
signal.


## Opening an issue when something trips

Pair the check with `peter-evans/create-issue-from-file` or a bare `gh`
call to surface violations as tracked work:

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

Capturing the report to a file keeps the issue body grounded in the
actual run rather than a hand-written summary that drifts.


## Caching the registry calls

`chill-out` keeps a small on-disk cache so repeated checks within a
short window don't re-hit the registry. In CI the cache directory
disappears at the end of every job, which is usually fine, but
projects with very wide dependency graphs can shave time off by
restoring it across runs:

```yaml
- uses: actions/cache@v4
  with:
    path: ~/.cache/chill-out
    key: chill-out-${{ runner.os }}-${{ hashFiles('**/uv.lock', '**/package-lock.json') }}
```

The lockfile hash invalidates the cache as soon as your dependencies
move, so you can't accidentally serve stale registry data after a
real upgrade.


## A real-world example

`chill-out` runs `chill-out check` against its own dependencies in
its CI pipeline. The check is part of the `qa/full` make target, so
the same command runs locally and in GitHub Actions. The wired-up
workflow lives at
[`.github/workflows/main.yml`](https://github.com/dusktreader/chill-out/blob/main/.github/workflows/main.yml)
and the make target is in the
[Makefile](https://github.com/dusktreader/chill-out/blob/main/Makefile).

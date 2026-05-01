# Quickstart

This guide takes you from a blank shell to a passing `chill-out check` in about five minutes. It assumes you already
have a Python or npm project lying around; if not, the smallest possible fixtures live under `examples/`.


## Install

```bash
pip install chill-out
```

If you live in `uv`-land:

```bash
uv tool install chill-out
```


## Run a check

`cd` into any project that has either a `pyproject.toml` or a `package.json` and run:

```bash
chill-out check
```

`chill-out` auto-detects the ecosystem, loads the cooldown thresholds, reads your lockfile, and prints a table of any
violations. Every package in the lockfile gets audited, principals and transitives alike, since the lockfile is what
actually gets installed. The exit code is `0` if everything is clear and `2` if at least one dependency is still
inside its cooldown window.

For pypi projects the lockfile is `uv.lock`, and it's required: if it's missing, `chill-out` asks you to run `uv lock`
first rather than guessing at what would be resolved. For npm projects the lockfile is `package-lock.json` (or its
copy under `node_modules/`), read via `npm list`.


## Read the output

A violation row looks roughly like this:

```text
2 cooldown violation(s) in 14 pypi package(s):
┌────────────────────────────────────┬───────────┬──────────────────────────────┐
│ Package                            │ Limit     │ Strategy                     │
├────────────────────────────────────┼───────────┼──────────────────────────────┤
│ requests = 2.31.0 (age 3d > 10d)   │ minor 10d │ requests -> 2.30.0 (45d old) │
│ urllib3 = 2.0.7 (age 1d > 10d)     │ minor 10d │ urllib3 -> 2.0.6 (60d old)   │
└────────────────────────────────────┴───────────┴──────────────────────────────┘
```

The "Package" column shows the violating dep with its installed version and the age vs limit called out. For a
transitive violation, the column renders as a tree from the principal down to the violating leaf, and the "Limit" column
shows a parallel tree so you can see each chain member's release type and threshold side by side. The "Strategy" column
tells you exactly which package to pin and to what version, so it's unambiguous that the fix targets the transitive
rather than rolling the principal back.


## Apply a fix

Run `chill-out fix` to rewrite your manifest and re-resolve:

```bash
chill-out fix
```

For npm projects, this pins each violating dep to its safe version directly in `dependencies` (the resolver hoists
transitive pins above whatever the principal asks for) and runs `npm install`. For Python projects, it pins each
violating dep to its safe version inside `pyproject.toml` and runs `uv lock`. When a transitive conflict can't be
resolved by hoisting alone, the principal gets rolled back to a version that admits the safe transitive.

After applying fixes, `chill-out` re-runs the check automatically so you can see whether the fix actually cleared every
violation. Pass `--no-recheck` to skip that second pass.


## Wire it into CI

The exit code is the contract:

```yaml
# .github/workflows/cooldown.yml
- name: Check dependency cooldown
  run: |
    pip install chill-out
    chill-out check --fast --quiet
```

If a violation appears, the job fails with exit code 2 and the table is in the log. See
[GitHub Actions](github-actions.md) for the full set of recipes: scheduled audits, separating cooldown signal from the
test matrix, opening issues from violations, and more.


## Speed knobs

`--fast` skips the safe-version lookup, which saves one extra registry round trip per violating package. Use it in CI
where you only care about pass/fail; the report still names the violating package, it just doesn't suggest a rollback
target.


## Next stops

- [Configuration](configuration.md) for tuning the thresholds
- [CLI](cli.md) for every flag
- [GitHub Actions](github-actions.md) for CI recipes
- [Programmatic API](api.md) for calling `chill-out` from your own code

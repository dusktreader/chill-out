# Quickstart

This guide takes you from a blank shell to a passing `chill-out check` in about
five minutes. It assumes you already have a Python or npm project lying around;
if not, the smallest possible fixtures live under `examples/`.


## Install

```bash
pip install chill-out
```

If you live in `uv`-land:

```bash
uv tool install chill-out
```


## Run a check

`cd` into any project that has either a `pyproject.toml` or a `package.json`
and run:

```bash
chill-out check
```

`chill-out` auto-detects the ecosystem, loads the cooldown thresholds, walks
your direct dependencies, and prints a table of any violations. The exit code
is `0` if everything is clear and `2` if at least one dependency is still
inside its cooldown window.


## Read the output

A violation row looks roughly like this:

```text
2 cooldown violation(s) in 14 pypi package(s):
┌─────────────┬───────────┬──────────────┬──────┬───────┬───────────────────────┐
│ Package     │ Installed │ Release Type │  Age │ Limit │ Suggested safe version │
├─────────────┼───────────┼──────────────┼──────┼───────┼───────────────────────┤
│ requests    │ 2.31.0    │ minor        │   3d │   10d │ 2.30.0 (45d old)      │
│ urllib3     │ 2.0.7     │ minor        │   1d │   10d │ 2.0.6 (60d old)       │
└─────────────┴───────────┴──────────────┴──────┴───────┴───────────────────────┘
```

The columns mean what they look like they mean. The "Suggested safe version"
is the newest released version that is older than what you have installed and
that has cleared its own cooldown window.


## Apply a fix

Pass `--fix` to let `chill-out` rewrite your manifest and re-resolve:

```bash
chill-out check --fix
```

For npm projects, this pins each violating dep to its safe version directly
in `dependencies` (the resolver hoists transitive pins above whatever the
principal asks for) and runs `npm install`. For Python projects, it pins each
violating dep to its safe version inside `pyproject.toml` and runs `uv lock`.
When a transitive conflict can't be resolved by hoisting alone, the principal
gets rolled back to a version that admits the safe transitive.


## Wire it into CI

The exit code is the contract:

```yaml
# .github/workflows/cooldown.yml
- name: Check dependency cooldown
  run: |
    pip install chill-out
    chill-out check --quiet
```

If a violation appears, the job fails with exit code 2 and the table is in the
log.


## Speed knobs

- `--fast` skips the safe-version lookup, which saves one extra registry round
  trip per violating package. Use it in CI where you only care about pass/fail.
- `--deep` includes transitive dependencies, not just direct ones. Slower, but
  much more thorough.


## Next stops

- [Configuration](configuration.md) for tuning the thresholds
- [CLI](cli.md) for every flag
- [Programmatic API](api.md) for calling `chill-out` from your own code

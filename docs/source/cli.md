# CLI

`chill-out` ships six subcommands. All of them accept `--root` to point at a project other than the current working
directory.


## `chill-out check`

Audits your lockfile against the configured cooldown windows. Reads every package in the lockfile, principals and
transitives alike, since the lockfile is what actually gets installed. Read-only: never mutates the project. This is
what you'll use 99% of the time.

```text
chill-out check [OPTIONS]

Options:
  -r, --root PATH         Project root (default: current directory).
  -e, --ecosystem TEXT    Force a specific backend (`npm` or `pypi`).
                          Auto-detected if omitted.
      --fast              Skip the safe-version lookup.
  -q, --quiet             Suppress the threshold table.
```

Exit codes:

| Code | Meaning                                                   |
| ---- | --------------------------------------------------------- |
| `0`  | All checked packages have cleared their cooldown.         |
| `2`  | At least one cooldown violation was found.                |
| `3`  | Configuration was unreadable or invalid.                  |
| `4`  | The ecosystem could not be detected or operated on.       |
| `5`  | A registry call failed and the report could not be built. |
| `99` | Internal error.                                           |


### Output

While registry calls are in flight, the CLI shows a spinner and an `M of N` progress bar so you can see how far along
the check is. The spinner clears as soon as the check finishes.

For each violation, the report prints the package, its release type, age, limit, and the suggested safe rollback target.
Transitive violations are rendered as a dependency tree showing the principal at the root and the violating package at
the leaf:

```text
fastapi 0.110.0
└── starlette 0.36.3
    └── anyio 4.3.0 (age 2d > 14d)
```

The intermediate node versions come from the project's lockfile, so the chain stays grounded in what is actually
installed.

----

## `chill-out fix`

Audits the project the same way `check` does, then rewrites manifests (and re-resolves lockfiles) to roll each violation
back to its safe version. Mutates the project, so opt in by command name.

```text
chill-out fix [OPTIONS]

Options:
  -r, --root PATH         Project root (default: current directory).
  -e, --ecosystem TEXT    Force a specific backend (`npm` or `pypi`).
                          Auto-detected if omitted.
      --fix-style TEXT    `exact` (default) pins to a single version; `compatible`
                          writes a range that allows future patch and minor
                          releases. Overrides the `fix_style` config field.
      --recheck/          After applying fixes, re-run the check to confirm
        --no-recheck      they took (default: --recheck).
      --cleanup/          Before running fresh fixes, remove pins chill-out wrote
        --no-cleanup      on previous runs whose underlying release has now
                          cleared cooldown (default: --cleanup). See the
                          [State file](state-file.md) page for details.
  -q, --quiet             Suppress the threshold table.
```

Exit codes match `check`. A successful fix that clears every violation exits `0`; surviving violations exit `2`.

The cleanup pass is driven by `.chill-out-state.json`, which `chill-out fix` writes alongside your manifests. See
[State file](state-file.md) for what it contains, how cleanup decides what to remove, and what to commit.

----

## `chill-out audit`

Read-only counterpart to the cleanup pass. `audit` opens `.chill-out-state.json`, asks the registry about every avoided
release, and tells you which pins are still earning their keep and which ones are ready to retire. Nothing on disk
changes. When you want chill-out to actually retire the pins it flags, run `chill-out fix` (the `--cleanup` pass is on
by default).

```text
chill-out audit [OPTIONS]

Options:
  -r, --root PATH         Project root (default: current directory).
  -e, --ecosystem TEXT    Force a specific backend (`npm` or `pypi`).
                          Auto-detected if omitted.
  -q, --quiet             Suppress the threshold table.
```

Each pin lands in one of four buckets:

| Bucket    | Meaning                                                                                         |
| --------- | ----------------------------------------------------------------------------------------------- |
| `stale`   | The avoided release has cleared its cooldown window. The pin can be retired.                    |
| `yanked`  | The avoided release was pulled from the registry. The pin can be retired with extra confidence. |
| `fresh`   | The avoided release is still inside its cooldown window. The pin is doing its job.              |
| `unknown` | The registry didn't return a publish date or the version is missing entirely. Review by hand.   |

Exit codes match `check`, with one addition:

| Code | Meaning                                                         |
| ---- | --------------------------------------------------------------- |
| `0`  | Every managed pin is fresh (or there's no state file to audit). |
| `7`  | At least one managed pin is stale or yanked.                    |

`unknown` does not flip the exit code by itself: the registry hiccupped, not the pin. Look at the report and decide.

When the state file is missing or holds zero pins, `audit` prints a single-line note and exits `0`. There's nothing to
audit, and that's not an error.

The `7` exit code (`STATE_STALE`) was picked specifically so CI can tell "your installed dependencies violate cooldown"
(`2`, from `check`) apart from "your state file has cleanup work waiting" (`7`, from `audit`). See the
[GitHub Actions](github-actions.md) page for a scheduled-cleanup-PR recipe that uses this distinction.

----

## `chill-out show-config`

Prints the resolved cooldown thresholds without running a check. Useful when you've added a config file and want to
confirm `chill-out` actually picked it up.

```text
chill-out show-config [OPTIONS]

Options:
  -r, --root PATH         Project root (default: current directory).
  -e, --ecosystem TEXT    Resolve the config for a specific ecosystem.
```


## `chill-out version`

Prints just the installed `chill-out` version. Useful for `--version`-style checks in scripts.


## `chill-out reset`

Escape hatch for when chill-out's bookkeeping has gotten in the way: the state file is corrupt, you've decided to
stop using chill-out, or you just want to start the next `fix` run with a clean slate. By default `reset` rolls back
every pin chill-out previously wrote into your manifests, then deletes `.chill-out-state.json`. Pass `--no-rollback`
to leave the pins in place and only forget about them.

```text
chill-out reset [OPTIONS]

Options:
  -r, --root PATH           Project root (default: current directory).
  -e, --ecosystem TEXT      Force a specific backend (`npm` or `pypi`) for the
                            rollback step. Auto-detected if omitted.
      --rollback/           Before deleting the state file, try to remove every
        --no-rollback       pin chill-out wrote (default: --rollback).
  -y, --yes                 Skip the confirmation prompt.
      --dry-run             Report what would happen without changing anything
                            on disk.
```

Rollback is best-effort: if the state file is unreadable or the ecosystem can't be detected, the rollback step is
skipped with a warning and the state file is deleted anyway. The state-file delete is the one operation `reset` is
contractually required to perform.

See [State file](state-file.md) for what `.chill-out-state.json` holds and why chill-out tracks it.


## Next stops

- [Configuration](configuration.md) for everything these flags can override
- [GitHub Actions](github-actions.md) for running these commands in CI
- [Examples](examples.md) for end-to-end recipes built around `check` and `fix`
- [Programmatic API](api.md) for calling the same logic from Python

# CLI

`chill-out` ships three subcommands. All of them accept `--root` to point at
a project other than the current working directory.


## `chill-out check`

Runs the cooldown check. This is what you'll use 99% of the time.

```text
chill-out check [OPTIONS]

Options:
  -r, --root PATH         Project root (default: current directory).
  -e, --ecosystem TEXT    Force a specific backend (`npm` or `pypi`).
                          Auto-detected if omitted.
      --deep              Include transitive dependencies.
      --fast              Skip the safe-version lookup.
      --fix               Apply fixes for violations that have a safe version.
      --recheck/          After --fix, re-run the check to confirm the fix
        --no-recheck      worked (default: --recheck).
  -q, --quiet             Suppress the threshold table.
```

Exit codes:

| Code | Meaning                                                |
|------|--------------------------------------------------------|
| `0`  | All checked packages have cleared their cooldown.      |
| `2`  | At least one cooldown violation was found.             |
| `3`  | Configuration was unreadable or invalid.               |
| `4`  | The ecosystem could not be detected or operated on.    |
| `5`  | A registry call failed and the report could not be built. |
| `99` | Internal error.                                        |


### Combining flags

`--fast` is incompatible with `--fix` (the fix planner needs the safe-version
lookup that `--fast` skips). Everything else combines.


### Output

While registry calls are in flight, the CLI shows a spinner and an
`M of N` progress bar so you can see how far along the check is. The
spinner clears as soon as the check finishes.

For each violation, the report prints the package, its release type, age,
limit, and the suggested safe rollback target. Transitive violations are
rendered as a dependency tree showing the principal at the root and the
violating package at the leaf:

```text
fastapi 0.110.0
└── starlette 0.36.3
    └── anyio 4.3.0 (age 2d > 14d)
```

The intermediate node versions come from the project's lockfile, so the
chain stays grounded in what is actually installed.


## `chill-out show-config`

Prints the resolved cooldown thresholds without running a check. Useful when
you've added a config file and want to confirm `chill-out` actually picked it
up.

```text
chill-out show-config [OPTIONS]

Options:
  -r, --root PATH         Project root (default: current directory).
  -e, --ecosystem TEXT    Resolve the config for a specific ecosystem.
```


## `chill-out version`

Prints just the installed `chill-out` version. Useful for `--version`-style
checks in scripts.

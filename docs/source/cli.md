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

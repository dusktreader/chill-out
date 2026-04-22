# chill-out examples

Each example is a self-contained, runnable script that exercises one slice of
the `chill-out` API. They are intentionally small so that they double as
copy-paste templates for real automation scripts.

| File                          | What it demonstrates                                        |
|-------------------------------|-------------------------------------------------------------|
| `cli_check.sh`                | Running the CLI against the current directory               |
| `programmatic_pypi.py`        | Calling `check_async` on a Python project from your code    |
| `programmatic_npm.py`         | Calling `check_async` on an npm project                     |
| `custom_config.py`            | Building a `CooldownConfig` in code instead of from a file  |
| `inspect_safe_versions.py`    | Using the pure cooldown helpers without the orchestrator    |

Run any Python example with:

```bash
uv run python examples/programmatic_pypi.py
```

The CLI example is a shell script:

```bash
bash examples/cli_check.sh
```

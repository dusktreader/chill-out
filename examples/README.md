# chill-out examples

Two flavors of example live here. The flat scripts are micro-templates that
exercise one slice of the API at a time. The `projects/` directory contains
two full example projects (one npm, one Python) with real-shaped manifests
and lockfiles, plus a demo script that runs the whole check + fix pipeline
end-to-end against them.

## API templates

| File                       | What it demonstrates                                        |
|----------------------------|-------------------------------------------------------------|
| `cli_check.sh`             | Running the CLI against the current directory               |
| `programmatic_pypi.py`     | Calling `check_async` on a Python project from your code    |
| `programmatic_npm.py`      | Calling `check_async` on an npm project                     |
| `custom_config.py`         | Building a `CooldownConfig` in code instead of from a file  |
| `inspect_safe_versions.py` | Using the pure cooldown helpers without the orchestrator    |

Run any Python example:

```bash
uv run python examples/programmatic_pypi.py
```

The CLI example is a shell script:

```bash
bash examples/cli_check.sh
```

## End-to-end project examples

| Project                        | Ecosystem | Walkthrough                                          |
|--------------------------------|-----------|------------------------------------------------------|
| `projects/npm-app/`            | npm       | direct pin + principal rollback on conflict          |
| `projects/python-app/`         | pypi      | direct pin + principal rollback on conflict          |
| `shop-monorepo/`               | npm       | shared transitive routed through workspace overrides |

Each `projects/` example has a real `package.json` / `pyproject.toml`, a
lockfile, a `.chill-out.yaml` config, and a `run_demo.py` that mocks the
registry and prints the full check report, the planned fix actions, and
the resulting manifest after `--fix` runs:

```bash
uv run python examples/projects/npm-app/run_demo.py
uv run python examples/projects/python-app/run_demo.py
```

`shop-monorepo/` is a live npm workspace fixture (no mocks). Install
its deps with `npm install` and then run chill-out against the
workspace root to see Tier 1 detection of a shared transitive plus
Tier 2 routing of the fix through the workspace-root `overrides`
field. See [`shop-monorepo/README.md`](shop-monorepo/README.md).

The narrated walkthrough lives in [`docs/source/examples.md`](../docs/source/examples.md).

# chill-out examples

Three flavors of example live here:

1. **API templates** at the top level. Single-purpose scripts that exercise one slice of the public API.
2. **Hermetic project demos** under `projects/`. Small but realistic single-root projects with mocked registries and
   stubbed subprocess calls. Run offline with one command and reproduce reliably from a fresh checkout.
3. **Live projects** under `live-projects/`. Real workspaces that need `npm install` (or equivalent) to set up. Used
   to reproduce resolution conflicts that mocks can't capture faithfully.

## API templates

| File                       | What it demonstrates                                       |
| -------------------------- | ---------------------------------------------------------- |
| `cli_check.sh`             | Running the CLI against the current directory              |
| `programmatic_pypi.py`     | Calling `check_async` on a Python project from your code   |
| `programmatic_npm.py`      | Calling `check_async` on an npm project                    |
| `custom_config.py`         | Building a `ChillOutConfig` in code instead of from a file |
| `inspect_safe_versions.py` | Using the pure cooldown helpers without the orchestrator   |

Run any Python example:

```bash
uv run python examples/programmatic_pypi.py
```

The CLI example is a shell script:

```bash
bash examples/cli_check.sh
```

## Hermetic project demos

| Project                | Ecosystem | Walkthrough                                 |
| ---------------------- | --------- | ------------------------------------------- |
| `projects/npm-app/`    | npm       | direct pin + principal rollback on conflict |
| `projects/python-app/` | pypi      | direct pin + principal rollback on conflict |

Each project has a real `package.json` / `pyproject.toml`, a lockfile, a `.chill-out.yaml` config, and a `run_demo.py`
that mocks the registry and prints the full check report, the planned fix actions, and the resulting manifest after
`chill-out fix` runs:

```bash
uv run python examples/projects/npm-app/run_demo.py
uv run python examples/projects/python-app/run_demo.py
```

The narrated walkthrough lives in [`docs/source/examples.md`](../docs/source/examples.md).

## Live projects

| Project                        | Ecosystem | What it reproduces                                   |
| ------------------------------ | --------- | ---------------------------------------------------- |
| `live-projects/shop-monorepo/` | npm       | shared transitive routed through workspace overrides |

Live projects aren't self-contained demos. They're real project layouts that you `npm install` (or equivalent) and then
point chill-out at. Used for reproducing resolution pathologies that only show up against a real package manager and
registry. See the per-project README for the setup steps and the expected chill-out behavior.

# Ecosystems

Each ecosystem backend knows three things: how to detect itself, how to list
its installed packages, and how to talk to its registry. They share a common
abstract interface so the rest of `chill-out` doesn't care whether it's
checking a Python or an npm project.


## Detection

Auto-detection is based on the file at the project root:

| Backend | Trigger file       |
|---------|--------------------|
| `npm`   | `package.json`     |
| `pypi`  | `pyproject.toml`   |

If both files are present (rare, but it happens in polyglot repos),
auto-detection refuses to guess. Pass `--ecosystem npm` or `--ecosystem pypi`
to disambiguate.


## npm

The npm backend uses `npm list --json` to enumerate installed packages and the
[npm registry](https://registry.npmjs.org) to fetch publish dates.

In the default mode, the backend only reports on packages declared as direct
dependencies in the root `package.json`. With `--deep`, it includes every
transitive package as well, and attributes each transitive to its principal via
a reverse-graph BFS over `package-lock.json`.

Workspaces are not supported in v1. If your repository is an npm workspace
monorepo, run chill-out from each sub-project's directory rather than from the
workspace root.

When the backend looks for `package-lock.json`, it tries the project root
first, then `node_modules/.package-lock.json` (the copy npm writes whenever it
installs), then walks up the directory tree trying both names at each level.
That last step lets a workspace member borrow its workspace root's lockfile so
transitive attribution still works when chill-out is run from a sub-project
that has no lockfile of its own. If no lockfile turns up anywhere on the way to
the filesystem root, the backend logs a warning and skips transitive
attribution for that run.

`--fix` writes safe versions into the root `package.json`:

- both principal and transitive violations land as direct entries in
  `dependencies` (the npm resolver hoists transitive pins above whatever the
  principal asks for)
- transitive conflicts that would put the resolver at odds with the principal
  trigger a principal rollback alongside the transitive pin

After editing, the backend runs `npm install` once to rewrite the lockfile.


## pypi

The pypi backend reads from `pyproject.toml` (the `[project.dependencies]`,
`[project.optional-dependencies]`, and `[dependency-groups]` tables) and pairs
each declared dep with its resolved version from `uv.lock`. If there is no
lockfile, it falls back to whatever pinned spec it can find in the requirement
strings (`==1.2.3`).

For deep mode, the backend walks every package in `uv.lock` and uses the
declared dependency edges to attribute each transitive to its principal. A
deep run requires `uv.lock`; without it the backend raises an error rather
than guess.

`--fix` rewrites `pyproject.toml` to pin each violating dep to its safe
version, then runs `uv lock` to regenerate `uv.lock`.

uv workspaces (the `[tool.uv.workspace]` block) are not supported in v1. If
your repository is a uv workspace, run chill-out from each member's directory
rather than from the workspace root.


## Adding a new backend

A backend is one class derived from `chill_out.Ecosystem`. The four methods
you implement:

```python
@classmethod
def detect(cls, root: Path) -> bool: ...

def load_installed(self, *, deep: bool = False) -> list[InstalledPackage]: ...

def make_client(self, http: httpx.AsyncClient) -> RegistryClient: ...

def apply_fixes(self, actions: list[FixAction]) -> list[str]: ...
```

`RegistryClient` is just a thin async wrapper around `httpx.AsyncClient` that
returns a `PackageInfo` with all known release timestamps. The orchestrator in
`chill_out.runner` does the rest.

Register your class in `chill_out/ecosystems/registry.py` and detection picks
it up automatically.

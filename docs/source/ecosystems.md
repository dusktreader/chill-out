# Ecosystems

Each ecosystem backend knows three things: how to detect itself, how to list its installed packages, and how to talk to
its registry. They share a common abstract interface so the rest of `chill-out` doesn't care whether it's checking a
Python or an npm project.


## Detection

Auto-detection is based on the file at the project root:

| Backend | Trigger file     |
| ------- | ---------------- |
| `npm`   | `package.json`   |
| `pypi`  | `pyproject.toml` |

If both files are present (rare, but it happens in polyglot repos), auto-detection refuses to guess. Pass
`--ecosystem npm` or `--ecosystem pypi` to disambiguate.


## npm

The npm backend shells out to `npm list --json` to enumerate every installed package (principals and transitives both,
exactly as they appear in the resolved tree) and queries the [npm registry](https://registry.npmjs.org) for publish
dates. Each transitive is attributed to its principal via a reverse-graph BFS over `package-lock.json`, so when a fresh
release surfaces deep in the tree the report still tells you which of your direct dependencies brought it in.

Workspaces are not supported in v1. If your repository is an npm workspace monorepo, run chill-out from each
sub-project's directory rather than from the workspace root.

When the backend looks for `package-lock.json`, it tries the project root first, then `node_modules/.package-lock.json`
(the copy npm writes whenever it installs), then walks up the directory tree trying both names at each level. That last
step lets a workspace member borrow its workspace root's lockfile so transitive attribution still works when chill-out
is run from a sub-project that has no lockfile of its own. If no lockfile turns up anywhere on the way to the filesystem
root, the backend logs a warning and skips transitive attribution for that run.

`chill-out fix` writes safe versions into the root `package.json`:

- both principal and transitive violations land as direct entries in `dependencies` (the npm resolver hoists transitive
  pins above whatever the principal asks for)
- transitive conflicts that would put the resolver at odds with the principal trigger a principal rollback alongside the
  transitive pin

After editing, the backend runs `npm install` once to rewrite the lockfile.


## pypi

The pypi backend reads `uv.lock` to enumerate every installed package, then consults `pyproject.toml` (the
`[project.dependencies]`, `[project.optional-dependencies]`, and `[dependency-groups]` tables) to decide which lockfile
entries are principals and which are transitives reached through them. That distinction isn't about which packages get
audited (every lockfile entry gets audited) but about where a fix gets written: principal rollbacks edit
`pyproject.toml`, transitive pins can, too, and the attribution keeps the rendered dependency chain honest.

`uv.lock` is required. If it's missing, the backend raises an ecosystem error asking you to run `uv lock` first. The
old fallback to pinned specs in `pyproject.toml` is gone: it was lying to you about what was actually installed.

`chill-out fix` rewrites `pyproject.toml` to pin each violating dep to its safe version, then runs `uv lock` to regenerate
`uv.lock`.

uv workspaces (the `[tool.uv.workspace]` block) are not supported in v1. If your repository is a uv workspace, run
chill-out from each member's directory rather than from the workspace root.


## Adding a new backend

A backend is two classes that satisfy the `chill_out.EcosystemDetector` and `chill_out.Ecosystem` Protocols. No
inheritance required; structural typing does the work.

```python
class MyDetector:
    def detect(self, root: Path) -> bool: ...


class MyEcosystem:
    kind: EcosystemKind = EcosystemKind.MINE
    root: Path

    def __init__(self, root: Path) -> None: ...
    def load_installed(self) -> list[InstalledPackage]: ...
    async def fetch_package(self, name: str, http: httpx.AsyncClient) -> PackageInfo | None: ...
    async def fetch_version_manifest(
        self, name: str, version: str, http: httpx.AsyncClient
    ) -> VersionManifest | None: ...
    def apply_fixes(self, actions: list[FixAction]) -> list[str]: ...
    # ... plus range_satisfies, parse_version, supports_overrides,
    #     apply_override_fixes, remove_managed_pin, regenerate_lockfile,
    #     workspace_topology
```

The orchestrator wraps your ecosystem in a `RegistryClient` that owns the cache + dedupe layer, so each
`fetch_*` call only has to translate one HTTP round-trip into a model. The runner in `chill_out.runner` does the
rest.

Register the `(detector, ecosystem_cls)` pair in `chill_out/ecosystems/registry.py` and detection picks it up
automatically.


## Next stops

- [Configuration](configuration.md) for setting `include_groups` and cooldown thresholds per ecosystem
- [Comparison](comparison.md) for how chill-out stacks up against other dependency tools
- [CLI](cli.md) for the commands that act on these ecosystems
- [Programmatic API](api.md) for driving an ecosystem adapter directly

# State file

When `chill-out fix` rewrites your manifests it leaves a trail behind in
`.chill-out-state.json` at the project root. The next `fix` run reads that trail, removes
the entries it can still identify, and only then runs the cooldown check. The result is a
fix loop where stale pins from months ago do not silently accumulate in `pyproject.toml`,
`package.json`, `[tool.uv.override-dependencies]`, or npm `overrides`.

If a package's cooldown has elapsed by the time the next run lands, the pin disappears.
If the violation is still live, the pin gets rewritten with up-to-date metadata. If you
have hand-edited the pin since chill-out wrote it, chill-out backs off and stops tracking
that entry, leaving your edit alone.


## Where it lives and what to commit

The file is written to the project root next to `pyproject.toml` or `package.json`. Commit
it. The team shares a view of what is currently pinned and why, and CI runs see the same
state your local runs do. Deleting it does not corrupt anything; chill-out simply forgets
which entries it owns and treats every survivor as user-authored on the next fix run.

If your team would rather keep the file out of the repo, add it to `.gitignore`. Be aware
that without the state file, chill-out cannot clean up its own pins, so they accumulate
over time until you remove them by hand.


## What's in it

The file is a single JSON object. Schema version 1 looks like this:

```json
{
  "schema_version": 1,
  "last_run_at": "2026-04-27T21:45:00Z",
  "ecosystem": "npm",
  "managed_pins": [
    {
      "package": "lodash",
      "ecosystem": "npm",
      "mechanism": "override",
      "manifest_path": "package.json",
      "pinned_spec": "4.17.20",
      "applied_at": "2026-01-15T14:22:00Z",
      "avoiding": {
        "version": "4.17.21",
        "release_type": "minor",
        "published_at": "2026-01-10T00:00:00Z",
        "cooldown_days": 10
      }
    }
  ]
}
```

The fields:

| Field             | Meaning                                                                          |
| ----------------- | -------------------------------------------------------------------------------- |
| `schema_version`  | The state file format version. The current value is `1`.                         |
| `last_run_at`     | Timestamp of the fix run that wrote this file.                                   |
| `ecosystem`       | The ecosystem detected for the project (`pypi` or `npm`).                        |
| `managed_pins`    | One entry per pin or override chill-out is currently tracking.                   |

Each pin entry:

| Field           | Meaning                                                                              |
| --------------- | ------------------------------------------------------------------------------------ |
| `package`       | The dependency the pin targets.                                                      |
| `ecosystem`     | The ecosystem the pin lives in.                                                      |
| `mechanism`     | `direct` for entries in the project's normal dependency tables; `override` for tree-wide overrides (`[tool.uv.override-dependencies]` or npm's `overrides`). |
| `manifest_path` | Path to the file holding the pin, relative to the project root.                      |
| `pinned_spec`   | The literal value chill-out wrote at the entry's site (e.g. `"lodash==4.17.20"` for pypi or `"^4.17.20"` for npm). |
| `applied_at`    | Timestamp the pin was last written.                                                  |
| `avoiding`      | Snapshot of the release that triggered the pin: version, release type, publish time, and the cooldown window in force at the time. |


## How cleanup decides what to do

Each entry in `managed_pins` falls into one of three buckets when the next fix run looks
at it:

`removed`
:   The entry is still at the recorded site, and its value matches `pinned_spec`. Chill-out
    deletes it from the manifest. If anything was removed in this pass, chill-out runs a
    single lockfile regeneration before moving on to the fresh check.

`drifted`
:   The entry is still at the recorded site, but its value differs from `pinned_spec`.
    Chill-out leaves the entry alone (you have clearly taken ownership of it), prints a
    warning, and drops the entry from state so it stops trying to manage it.

`orphan`
:   The entry's recorded site no longer references the package at all. Chill-out drops the
    entry from state silently.

After cleanup the fresh check runs. Anything chill-out fixes in this round becomes a new
entry in the rewritten state file. If no entries remain at the end of the run, the state
file is deleted rather than left behind as a no-op.


## Skipping cleanup

Pass `--no-cleanup` to skip the cleanup pass for one run. The state file is still loaded
and rewritten, but no removals are attempted. This is useful when you want to reapply a
fresh round of fixes without touching what chill-out has already pinned, for example
during a debugging session or a one-off audit.

```sh
chill-out fix --no-cleanup
```


## When the file is invalid

The state file is validated against a strict schema on every load. If the JSON is malformed, the file is unreadable,
required fields are missing, types are wrong, or the `schema_version` is one chill-out doesn't understand, the run
halts with a typed error pointing at the offending field. Chill-out never silently treats a bad state file as empty;
that path used to orphan pins forever, so it had to go.

If you cannot or do not want to repair the file by hand, run `chill-out reset --no-rollback` to delete it without
touching your manifests. The next `fix` run will treat your manifests as if nothing was previously tracked. Pins
already written stay in place; chill-out just stops considering them its own. See
[`chill-out reset`](cli.md#chill-out-reset) for the full subcommand.


## Schema versioning

A future version of chill-out may bump `schema_version`. When a current chill-out reads a state file with a version
it doesn't recognise (e.g. a file written by a newer release), it raises `StateSchemaVersionError` and exits. The
fix: upgrade chill-out to a version that knows the new schema, or run `chill-out reset` to discard the file. Either
way, no data is silently lost.


## Manual cleanup

Deleting `.chill-out-state.json` by hand is always safe. Chill-out will treat the next run as if nothing was
previously tracked. Pins that already exist in your manifests stay where they are; chill-out just stops considering
them its own.

For a guided cleanup, use [`chill-out reset`](cli.md#chill-out-reset). By default it also rolls back every pin
chill-out wrote into your manifests before deleting the state file, leaving the project in roughly the shape it had
before chill-out was first run. Pass `--no-rollback` if you want to keep the pins and only forget about them.

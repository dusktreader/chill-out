# TODO

Post-publication tasks. Checked-in so they don't fall out of working memory; not
shipped to users.

## Build a `dusktreader/chill-out-action` GitHub Action

The [GitHub Actions](docs/source/github-actions.md) page currently asks every
user to wire chill-out into their workflows by hand: install the CLI with
`uv tool install`, then call `chill-out check` (or `audit`, or `fix`) as a
shell step. That works, but a dedicated composite action would shrink the
common case to a couple of lines:

```yaml
- uses: dusktreader/chill-out-action@v1
  with:
    command: check
    quiet: true
```

Sketch of what the action should cover:

- Inputs for `command` (`check`, `audit`, `fix`, `show-config`), `root`,
  `ecosystem`, `quiet`, `fast`, `fix-style`, `recheck`, `cleanup`, plus a
  `version` input that pins which chill-out release to install.
- Composite-action implementation (no Docker), so it runs on the same runner
  as the rest of the workflow and inherits cached toolchains.
- Install step that uses `uv tool install chill-out==${VERSION}`. Falls back
  to a pinned latest when `version` is omitted.
- Outputs for the report path and the exit code, so downstream steps can
  read them without re-running the command.
- Marketplace listing once the action has at least one tagged release and a
  README that links back to the chill-out docs site.

Once the action exists, update `docs/source/github-actions.md`: keep the
hand-rolled recipes for users who want them, but lead with the action.
Reduce the scheduled-cleanup-PR recipe to a four-step workflow.

Reason this is post-publication: the CLI has to be on PyPI and reachable
under a stable name before the action can install it. Ship `chill-out` first,
then build the action against the released CLI.

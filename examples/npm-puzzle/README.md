# npm-puzzle

An anonymized npm workspace that reproduces a real-world dependency
conflict that single-root cooldown checkers can't fix on their own.

## The puzzle

Three workspace members share a transitive dependency on
`@smithy/util-endpoints`:

- `api/` deliberately pins `@smithy/util-endpoints` to `3.3.4` (a
  realistic case: an old AWS SDK demanded it and the pin was never
  cleaned up).
- `worker/` pulls in a newer AWS SDK that brings in
  `@smithy/util-endpoints@3.4.0+` through `@aws-sdk/credential-providers`.
- `web/` depends on `@shop/api`, so it inherits the same pin transitively.

`npm install` reports an `invalid` peer for the version that worker
wants to hoist. Running cooldown analysis reveals the pinned `3.3.4`
sits below the cooldown window. A direct pin in `api/package.json` only
moves the version `api` resolves to: the copy hoisted into worker's
subtree stays put. The only way to force one version everywhere is the
`overrides` field in the workspace root's `package.json`, which is
exactly what chill-out's Tier 2 fix path applies automatically when it
sees a violation shared across two or more members.

## How chill-out should handle it

Run from any member directory:

```
chill-out check --root examples/npm-puzzle/api --deep --fix
```

Expected: chill-out detects `@smithy/util-endpoints@3.3.4` as a
violation, sees it's owned by `api`, `worker`, and `web`, and writes
the fix to `examples/npm-puzzle/package.json`'s `overrides` field
instead of editing the member's `dependencies`.

## Setup

```
cd examples/npm-puzzle
npm install
```

The lockfile and `node_modules/` are gitignored; chill-out reads them
at runtime to walk the dependency graph.

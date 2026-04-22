# shop-monorepo

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
wants to hoist. Running cooldown analysis flags the older copy as
sitting below the cooldown window. Editing
`api/package.json`'s `dependencies` only moves the version that `api`
itself resolves to: the copy hoisted into worker's subtree stays put.
The only way to force one version everywhere is the `overrides` field
in the workspace root's `package.json`, which is exactly what
chill-out's Tier 2 fix path applies automatically when it sees a
violation shared across two or more members.


## How chill-out should handle it

Run from the workspace root:

```
chill-out check --root examples/shop-monorepo --deep --fix
```

Expected behavior: chill-out detects `@smithy/util-endpoints@3.4.2`
as a violation, sees it's owned by both `@shop/api` and `@shop/worker`,
and writes the fix to `examples/shop-monorepo/package.json`'s
`overrides` field instead of editing any member's `dependencies`. The
strategy column shows
`(shared: @shop/api, @shop/worker; will use overrides)` to flag the
shared-routing decision before the fix runs.


## Setup

```
cd examples/shop-monorepo
npm install
```

The lockfile and `node_modules/` are gitignored; chill-out reads them
at runtime to walk the dependency graph.

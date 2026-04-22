# npm-app: chill-out example project

A self-contained npm project that chill-out can audit. The fixture is
intentionally constructed so a deep check finds two violations:

- `chalk@5.4.0` is a fresh direct dependency that sits inside the cooldown
  window, so chill-out proposes pinning it back to `5.3.0`.
- `lodash.merge@4.6.3` is a fresh transitive of `lodash`. The installed
  `lodash@4.17.21` declares its merge transitive with a tight range that
  doesn't admit `4.6.2`, so chill-out's principal-rollback path picks an
  older `lodash` whose declared range does and emits the override along with
  the principal pin.

Run the demo from the repository root:

```bash
uv run python examples/projects/npm-app/run_demo.py
```

The full walkthrough lives in `docs/source/examples.md`.

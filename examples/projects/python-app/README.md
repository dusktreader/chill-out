# python-app: chill-out example project

A self-contained Python project that chill-out can audit. The fixture is constructed so a lockfile audit finds two
violations:

- `httpx==0.27.0` is a fresh direct dependency inside the cooldown window; chill-out proposes pinning it back to
  `0.26.0`.
- `anyio==4.3.0` is a fresh transitive that fastapi pulls in. The installed `fastapi==0.110.0` declares `anyio>=4.3,<5`,
  which excludes the safe `anyio==4.2.0`. Chill-out's principal-rollback path picks an older fastapi (`0.109.2`) whose
  declared range admits `4.2.0`, then emits both the principal pin and a direct pin of anyio.

Run the demo from the repository root:

```bash
uv run python examples/projects/python-app/run_demo.py
```

The full walkthrough lives in `docs/source/examples.md`.

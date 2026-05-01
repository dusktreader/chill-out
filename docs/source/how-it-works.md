# How it works

`chill-out` is a small pipeline glued together from a handful of independent pieces, each of which you can read in
isolation. This page walks through what happens between `chill-out check` and the report it prints, so you know what
to expect, where to look when something goes sideways, and how to extend it if you want to.


## The pipeline at a glance

Every run, whether `check` or `fix`, follows the same four phases:

1. **Detect** the ecosystem (npm or pypi) by looking for telltale files in the project root.
2. **Load** every installed package from the lockfile: principals and transitives together.
3. **Ask the registry** when each version was published and how it relates to other releases of the same package.
4. **Evaluate** each release against the configured cooldown windows and assemble a report.

`fix` adds a fifth phase: turn the report's violations into a list of `FixAction`s, write them to disk, and re-run the
check to confirm the resolver actually picked up the new pins.

Each phase lives in its own module. The high-level orchestration is in `chill_out.runner`; the cooldown math is in
`chill_out.cooldown`; the per-ecosystem details are in `chill_out.ecosystems.npm` and `chill_out.ecosystems.pypi`. None
of the phases know more than they need to about the others, which makes the pipeline easy to reason about and easy to
add a third ecosystem to.


## Detecting the ecosystem

`detect_ecosystem(root)` looks for the marker files each backend declares. For npm that's `package.json`; for pypi
that's `pyproject.toml`. The first ecosystem that says "yes, that's me" wins. If you have both (a Python project that
also bundles a JS frontend), pass `--ecosystem` to force the choice; otherwise detection picks whichever shows up first
in the registry.

The `Ecosystem` Protocol in `chill_out.ecosystems.backend` defines the contract every backend honors:

```python
class Ecosystem(Protocol):
    kind: EcosystemKind
    root: Path
    def load_installed(self) -> list[InstalledPackage]: ...
    async def fetch_package(self, name: str, http: httpx.AsyncClient) -> PackageInfo | None: ...
    async def fetch_version_manifest(
        self, name: str, version: str, http: httpx.AsyncClient
    ) -> VersionManifest | None: ...
    def apply_fixes(self, actions: list[FixAction]) -> list[str]: ...
```

Detection of which backend applies to a given project root lives on a separate `EcosystemDetector` Protocol so the
registry can probe candidates without constructing a backend instance up front:

```python
class EcosystemDetector(Protocol):
    def detect(self, root: Path) -> bool: ...
```

That's the whole interface. Adding a third ecosystem (cargo, gem, hex, whatever) is a matter of writing a class that
satisfies these protocols (no inheritance required) and registering it. The runner doesn't care which one it's
holding.


## Reading the lockfile

Each backend reads the project's lockfile and produces a flat list of `InstalledPackage` records: `(name, version,
ecosystem)`. The npm backend shells out to `npm list --json` (which reads `package-lock.json` under the hood); the pypi
backend parses `uv.lock` directly.

Every package in the lockfile makes it into that list. The lockfile is what actually gets installed, so auditing any
subset of it would be lying about the threat surface. A `requests` declared in `pyproject.toml` matters only insofar
as it shows up in `uv.lock`, and a transitive nobody declared shows up in the lockfile all the same, ready to run code
in your environment like any other dependency. Supply chain attacks surface as brand-new releases of some package,
somewhere in that tree. That's why "the lockfile end-to-end" is the right unit to audit.

`uv.lock` is required for the pypi backend. If it's missing, the backend raises an ecosystem error asking you to run
`uv lock` first rather than guessing at what would be resolved. The old fallback to pinned specs in `pyproject.toml` is
gone: it was answering a different question than the one a cooldown audit actually asks.

The distinction between "principal" and "transitive" is still meaningful, but it matters for fix planning rather than
for the audit. A principal is a package you declared directly; a transitive is pulled in by something else. We need to
know which is which so we can pin to the right file when fixing (and so the dependency tree rendered for transitive
violations points at the right principal at the top of the chain).


## Asking the registry

`check_async` builds a `RegistryClient` for the detected ecosystem and fans out one async request per package, gated by
a semaphore so a project with 200 dependencies doesn't open 200 simultaneous sockets. The default concurrency is high
enough to keep wall-clock time short on a typical project but low enough to play nicely with rate-limited registries.

Each response gets normalized into a `PackageInfo`: a dict of every published version mapped to its publish timestamp.
That's the only piece of registry data the cooldown engine needs. Any other metadata the registry hands back (downloads,
licenses, deprecation flags) is dropped on the floor; the pipeline stays narrow on purpose.

### Request deduplication

`RegistryClient` wraps an `Ecosystem`'s registry methods with an in-memory dedupe layer. Within a single run, the
first lookup for a given package (or a given `(package, version)` manifest) hits the network; every subsequent lookup
for the same key returns the stored result without another round trip. Concurrent callers asking for the same key while
a fetch is still in flight share that one fetch instead of racing duplicate requests.

The cache lives for the lifetime of the process and nothing more. There's no on-disk store and no cross-run
persistence: each invocation starts empty and warms up as it goes. That keeps the pipeline stateless from the user's
perspective (no cache directory to manage, no staleness to worry about) while still saving real work inside a single
run, since the same transitive package often gets reached through several chains in a wide graph.


## Classifying releases

For each installed version, `release_type` (in `chill_out.cooldown`) categorizes it as `MAJOR`, `MINOR`, or `PATCH`
based on its position in the release sequence:

- `1.0.0` -> major (everything to the right is zero)
- `1.4.0` -> minor (patch is zero, minor is not)
- `1.4.7` -> patch (everything else)

`release_type` doesn't parse the version itself. It takes a `VersionParser` callable supplied by the active ecosystem
(see [Ecosystem-owned version parsing](#ecosystem-owned-version-parsing) below) and works against the resulting
`ParsedVersion`. Anything the parser rejects (date-based versions, vendor suffixes, anything too creative) falls back
to a `DEFAULT` release type with its own configurable threshold. The classification is intentionally simple:
chill-out doesn't try to divine intent from changelogs or tags, just from the version number itself.

`is_within_cooldown` then compares the publish timestamp to the configured threshold for that release type. If the
release is younger than its threshold, it's a violation; otherwise it's clear.


## Ecosystem-owned version parsing

Version strings look superficially uniform across ecosystems but they aren't. npm enforces strict three-segment semver
and treats anything else as invalid. PyPI follows PEP 440, which permits two-segment releases (`idna 3.12`), epochs
(`1!2.0`), post-releases (`1.0.post1`), and a long tail of things semver would reject outright. Trying to share one
parser between the two would force the engine to either pick a least-common-denominator (and silently misclassify
real releases) or invent a new dialect that matches neither registry.

So each ecosystem owns its own parser. `NpmEcosystem.parse_version` runs the input through a semver library;
`PypiEcosystem.parse_version` runs it through `packaging.Version`. Both produce a common `ParsedVersion` with the four
pieces of information the cooldown engine needs: major / minor / micro segments, a pre-release flag, and an opaque
`sort_key` that defines what "newer than" means in that ecosystem's universe. The engine never sees the raw version
string except to round-trip it back into manifests verbatim, which means a safe version of `idna 3.12` writes back as
`3.12`, not as `3.12.0` or some other helpfully-canonicalized variant.

This split is what fixed an early bug where `idna 3.12` (a perfectly valid PEP 440 release) was getting
misclassified as `DEFAULT` because the shared parser only spoke semver. Now the pypi parser handles it as the patch
release it actually is, and the cooldown windows apply correctly.


## Picking a safe version

When a violation has a usable safe rollback target, the report includes it. `find_safe_version` walks the registry's
release history for the same package, looking for the newest version that:

1. Is **strictly older** than the currently installed version. We're rolling back, never forward.
2. Has **cleared its own cooldown** window. A six-day-old patch isn't safer than the one we're trying to replace.
3. Is **not a pre-release**. Betas and release candidates aren't valid rollback targets.

The same `VersionParser` from the previous section drives both the ordering ("strictly older") and the pre-release
filter, so the rollback search respects the same ecosystem semantics that classification did. The newest version that
passes those three filters becomes the safe version. If nothing qualifies, the violation is reported without a
rollback suggestion and `fix` will mark it `UnfixableViolation`.

Note that `--fast` skips this lookup entirely. With `--fast`, the report still tells you what's in cooldown, but it
won't suggest where to retreat to. Useful when you're using chill-out as a pure pass/fail gate and don't need the
rollback guidance.


## Transitive violations and the dependency tree

A transitive package can show up as a violation even though you don't depend on it directly. The report attributes the
violation to the principal (the direct dependency that pulled the transitive in) and renders the chain so it's clear
who to talk to:

```text
fastapi 0.110.0
└── starlette 0.36.3
    └── anyio 4.3.0 (age 2d > 14d)
```

The intermediate node versions are pulled from the project's lockfile, so the chain reflects what's actually installed,
not what the registry currently lists as the latest. If you upgraded `fastapi` an hour ago, the chain shows the version
the lockfile resolved to.


## Conflict-aware rollback

The interesting case is when a transitive violation can't be fixed by hoisting a direct pin. Suppose `anyio 4.3.0`
violates cooldown and the safe version is `anyio 4.2.0`, but the principal `starlette 0.36.3` declares
`anyio>=4.3,<5`. A direct pin to `4.2.0` won't take: the resolver will refuse it as soon as it sees starlette's
declared range.

`find_safe_principal_version` handles this. It walks older versions of starlette, looking for one that:

1. Has cleared its own cooldown.
2. Is **not** a pre-release.
3. Declares a range for `anyio` that the safe `4.2.0` satisfies, **or** doesn't declare `anyio` at all (in which case
   it can't pull the violating version in by accident).

If it finds one, the fix planner emits two `FixAction`s: a rollback of starlette and a direct pin of anyio. Both legs
render as exact pins regardless of the configured `fix_style`, so the resolver can't drift back into the conflict on
the next install. This is also why the principal/transitive distinction still earns its keep: to write a paired
rollback we need to know which dependency sits at the top of `pyproject.toml` (or `package.json`) and which one is
reached through it.

If no qualifying principal version exists, the violation lands in the `UnfixableViolation` bucket with a structured
reason explaining why, so the report can give the maintainer something better than "can't fix this, sorry."


## Override fallback (npm)

npm's hoisting rules and sticky lockfiles can defeat even a paired rollback. You pin a transitive at the safe version,
re-run `npm install`, and the lockfile cheerfully reinstates the old one because some other package up the tree still
prefers it.

After the post-fix re-check, if any violation we **just attempted to fix** is still present, the npm backend falls back
to writing the safe version into the workspace `overrides` block. Overrides are npm's resolver-level "I really mean it"
mechanism: they win over every other declared range. The pipeline re-checks one more time after writing overrides; if
the violation still survives, the report tells you so with enough context to fix by hand.

The pypi backend doesn't currently have an equivalent fallback because uv's resolver respects direct pins more reliably
in practice. If a case shows up where it doesn't, the same shape would work: a `[tool.uv.constraints]` block or
similar.


## Why these choices

A few decisions are worth calling out, mostly because they constrain what chill-out tries to be.

### Lockfile end-to-end, always

Cooldown gives you the most protection when every package that will actually run in your environment has cleared the
window. The lockfile is the only artifact that lists them all (principals, transitives, platform-specific resolutions,
everything), so the lockfile is what gets audited. There's no "direct deps only" mode and no "fast path that skips the
tree": the honest answer to "is my deploy safe?" needs the whole tree.

This is also why `uv.lock` is required on pypi. Reading pinned specs out of `pyproject.toml` and pretending that's what
got installed is a worse kind of wrong than a clear error message.

### Per-ecosystem release classification

Pre-1.0 versions, local versions, and vendor-tagged builds don't get major / minor / patch treatment; they fall
through to `DEFAULT`. That's deliberate. Trying to assign release-type semantics to a string like
`2.0.0a1+local.20240101` is a guessing game, and a wrong guess silently relaxes the threshold for that package. A
single `default` knob is honest about the lack of structure.

What does get a real classification is whatever the active ecosystem's parser recognizes. npm's parser is strict
semver; pypi's parser is PEP 440, which means PyPI releases get classified using PyPI's own rules rather than a
foreign approximation. The engine itself stays ecosystem-agnostic, so adding a third backend means writing a third
parser, not changing the cooldown math.

### Exact pins by default for `fix_style`

The default is `exact` because exact pins are the most aggressive way to make sure the resolver actually picks the
version you asked for. `compatible` style writes a range that allows future patch and minor releases, which is friendly
for routine maintenance but assumes the resolver will pick the safe version on the next `install` instead of jumping
forward to whatever's newest. Both work; exact is the cautious default.

### In-memory cache, not on-disk

The registry cache is deliberately per-process. A persistent on-disk cache would save network traffic across runs, but
it would also introduce a staleness window (packages do get yanked, timestamps do occasionally get corrected) and a
cache directory the user has to know about. Keeping the cache in-memory means every run sees fresh data, the dedupe
still covers the expensive case (repeated lookups within one invocation), and there's nothing to invalidate when
something weird happens upstream.

### No global wall-clock cap

There's no top-level "give up after 30 seconds" timer in the pipeline. Each registry call has its own timeout (defaults
to a sane value, configurable via `httpx`), and the parallel fanout keeps the worst case bounded by the slowest single
package, not by the count of packages. Adding a global deadline would make the pipeline harder to reason about for very
little benefit.


## Next stops

- [Configuration](configuration.md) for the cooldown thresholds, dependency-group filters, and fix-style options the
  pipeline reads at startup
- [Ecosystems](ecosystems.md) for the per-backend implementation details glossed over here
- [Programmatic API](api.md) for calling each phase directly from Python
- [Reference](reference.md) for the auto-generated module documentation

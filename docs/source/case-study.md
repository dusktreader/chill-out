# Case study: ninety days at the shoreline

This page follows a single small project across three months of regular dependency churn. It's a fictional but
realistic narrative: one developer, one Python repo, dependabot wired up for weekly upgrade PRs, and `chill-out`
guarding the lockfile. Each episode picks up some calendar time after the previous one and shows what changes,
what `chill-out` says about it, and why.

The names of the packages and their versions are invented. The shape of the cooldown story isn't.


## The project

Imagine a small CLI tool called `tikibar`: a recipe manager for beach-bar cocktails, the kind of project where a
maintainer's idea of a unit test involves a blender and a paper umbrella. It's a single Python package with three
direct runtime dependencies, one dev dependency, and a `pyproject.toml` that looks roughly like this:

```toml
[project]
name = "tikibar"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "blender>=2.4,<3",
    "pineapple>=1.8,<2",
    "paper-umbrella>=0.9,<1",
]

[dependency-groups]
dev = [
    "pytest>=8",
]
```

You install `chill-out` once and drop a `.chill-out.yaml` next to the manifest:

```yaml
fix_style: compatible
cooldown:
  major: 60
  minor: 14
  patch: 7
  default: 7
```

This says: a freshly-cut major version has to age sixty days before you'll trust it, minors fourteen, patches a week.
The `compatible` fix style means when `chill-out` does write a pin, it writes it as a range that future safe patches
and minors can satisfy without another fix pass. Cautious enough for a side project; flexible enough not to demand
manual cleanup every Monday.

For the sake of concrete dates in the artifacts that follow, anchor day 0 at **Wednesday, April 15, 2026**. Day 14
is April 29, day 30 is May 15, day 60 is June 14, and day 90 is July 14.


## Day 0: laying the lockfile

You've cloned a fresh repo, written a few lines of code that import from the three principals, and you're ready to
resolve the lockfile for the first time. `uv lock` walks the declared ranges, picks a concrete version for every
direct and transitive dependency, and writes the result to `uv.lock`. That file is what `pip install` (or `uv sync`)
will actually pull onto disk in CI and in production, so it's the file `chill-out` cares about.

Run it:

```bash
chill-out check
```

Output:

```text
No cooldown violations across 17 pypi package(s).
```

Seventeen packages got audited: the three direct dependencies you declared, one dev dependency, and thirteen
transitives that came along for the ride when the resolver expanded those principals into a full graph. Nothing in
the report is particularly fresh, which is the whole point of starting from stable principals: their latest releases
have been in the wild long enough that their own transitives have settled too. With the check passing, your lockfile
becomes a known-good baseline. Bar's open.

At this point your project root contains the manifest you wrote, the lockfile `uv` generated, and that's it. There
is no `.chill-out-state.json` yet, because chill-out only writes one when it has a managed pin to remember. A check
that finds nothing to fix has nothing to remember. The on-disk state of the project is exactly:

```toml
# pyproject.toml (relevant slice)
[project]
dependencies = [
    "blender>=2.4,<3",
    "pineapple>=1.8,<2",
    "paper-umbrella>=0.9,<1",
]
```

Hold this picture in mind. Everything that follows changes one or both of two files: this manifest, and the state
file that springs into existence the first time chill-out writes a pin.


## Day 14: the first dependabot batch

Two weeks in, dependabot opens four PRs, one per principal. The branch CI runs `chill-out check` against each
proposed lockfile. Three pass quietly. The fourth fails:

```text
1 cooldown violation in 17 pypi package(s):
┌─────────────────────────────────────────────────┬───────────┬─────────────────────────────────────┐
│ Package                                         │ Limit     │ Strategy                            │
├─────────────────────────────────────────────────┼───────────┼─────────────────────────────────────┤
│ blender = 2.5.0 (age 3d > 14d)                  │ minor 14d │ blender -> 2.4.7 (21d old)          │
└─────────────────────────────────────────────────┴───────────┴─────────────────────────────────────┘
```

`blender 2.5.0` shipped three days ago. Your minor-release threshold is fourteen, so this PR has eleven days left to
wait. The `Strategy` column already names a safe alternative: `2.4.7`, twenty-one days old, comfortably past its
patch-level threshold.

Two reasonable choices. The first is to do nothing: leave dependabot's PR open, let it sit until the cooldown window
clears, and let CI flip green on its own when the next scheduled run reaches the same lockfile against an aged-out
`2.5.0`. The second is to take over the branch and replace the unsafe upgrade with the safe one. Check out
dependabot's branch locally, run `chill-out fix` to rewrite the manifest and lockfile to the safe target, then push
the result back onto the same branch:

```bash
gh pr checkout 42
chill-out fix
git add pyproject.toml uv.lock
git commit -m "use blender 2.4.7 to clear cooldown"
git push
```

This matters because `main` and the PR have to stay in sync about the decision: if you applied the fix directly to
`main` and closed the dependabot PR, the next dependabot run would see no pin change and happily reopen the exact
same `2.5.0` proposal a week later. By landing the fix on the PR branch and merging from there, dependabot sees its
own PR get accepted and won't propose `blender` again until something newer than `2.4.7` ships.

Because the config says `fix_style: compatible`, the rewrite preserves the existing `>=2.4` lower bound and caps the
range below the next major:

```toml
"blender>=2.4,<3"  # unchanged; resolver picks 2.4.7
```

In this case the existing range already admits `2.4.7`, so the fix is a no-op on the manifest itself: the lockfile
gets refreshed to pin the safe version, and the PR's diff now shows a clean `2.4.7` upgrade instead of the unsafe
`2.5.0` one. CI re-runs against the updated branch, the cooldown check passes, and you merge it. Eleven days from
now, when `2.5.0` ages out of its window, dependabot will open a fresh PR proposing the bump from `2.4.7` to whatever
the latest safe release is at that point, and it'll sail through without intervention.

The other three PRs merge without any fuss. Tide's been out long enough.

The on-disk state of the project after the day-14 dust settles:

```toml
# pyproject.toml (relevant slice; unchanged from day 0)
[project]
dependencies = [
    "blender>=2.4,<3",
    "pineapple>=1.8,<2",
    "paper-umbrella>=0.9,<1",
]
```

Still no `.chill-out-state.json`. The state file exists to remember manifest pins chill-out wrote, and chill-out
hasn't written any: with `fix_style: compatible` and a range that already admits the safe version, there was nothing
to write. The lockfile carries the decision (it now pins `blender==2.4.7`), and that's enough to make CI deterministic
until the next dependabot pass. The "audit trail" benefit kicks in only once chill-out starts editing manifests on
your behalf, which it hasn't needed to yet.


## Day 30: a yanked release

Three weeks later, a security researcher discovers that `pineapple 1.9.0`, published twenty-five days ago, was shipped
by a maintainer whose publishing token had been compromised. The package was modified post-publish to exfiltrate
environment variables on import: a bad batch slipped into the supply, and several downstream projects had already
served it to production. The maintainers yank the release and cut `1.9.1` as a clean replacement.

You hear about this on a Friday afternoon, panic for thirty seconds, and check your lockfile.

```bash
chill-out check
```

```text
No cooldown violations across 17 pypi package(s).
```

Your `pineapple` is still pinned at `1.8.4`. The `1.9.0` release came out twenty-five days ago, but your minor
threshold is fourteen, so the dependabot PR that proposed `1.9.0` would have failed cooldown for the first eleven of
those days. By the time it cleared, it would have merged exactly once: today, when `chill-out` would have noticed
that `1.9.0` was now eleven days clean. Except `1.9.0` doesn't exist anymore.

You don't have to take chill-out's word for it. The pypi backend reads the per-artifact `yanked` flag from the JSON
API and collapses it to a per-release decision (a release is yanked when every artifact for that version is yanked,
which is what pip and uv do). The cooldown planner skips yanked candidates when picking a safe version, so even if
your config's threshold had been zero days, a fresh `chill-out fix` pass today would not have proposed `1.9.0`. It
would have rolled forward to `1.9.1`, the clean replacement, which was published two hours ago and is well inside its
own cooldown window.

The system did its job: the cooldown gave the community time to notice the spoiled batch, and the package manager's
yank metadata closed the loop. You shipped no malicious code. You also did nothing differently. That's the entire
point.


## Day 60: principal rollback through a transitive

Two months in. Dependabot opens a PR that bumps `blender` from `2.4.7` to `2.6.0`. The new release ships with a fresh
batch of `crushed-ice`, a transitive that lives inside the blender, and the branch fails cooldown:

```text
2 cooldown violations in 17 pypi package(s):
┌──────────────────────────────────────────────────────┬───────────┬─────────────────────────────────────────────────┐
│ Package                                              │ Limit     │ Strategy                                        │
├──────────────────────────────────────────────────────┼───────────┼─────────────────────────────────────────────────┤
│ blender = 2.6.0 (age 5d > 14d)                       │ minor 14d │ blender -> 2.5.4 (32d old)                      │
│ blender = 2.6.0                                      │ minor 14d │ blender -> 2.5.4 (32d old)                      │
│ └─ crushed-ice = 1.4.0 (age 5d > 14d)                │ minor 14d │ crushed-ice -> 1.3.8 (40d old)                  │
└──────────────────────────────────────────────────────┴───────────┴─────────────────────────────────────────────────┘
```

Two violations from one PR. The principal `blender 2.6.0` is fresh, and so is its newly-required transitive
`crushed-ice 1.4.0`. The tree rendering in the `Package` column makes the relationship explicit: `crushed-ice` is
violating because `blender` pulled it in.

Run the fix:

```bash
chill-out fix
```

Output:

```text
applying fixes:
  - pinned blender -> 2.5.4
  - pinned crushed-ice -> 1.3.8
  - ran: uv lock
re-running check ...
No cooldown violations across 17 pypi package(s).
```

The planner walked back through `blender`'s release history looking for the most recent version whose declared
dependency range admits a safe `crushed-ice`. `blender 2.5.4` declares `crushed-ice>=1.3,<2`, which `1.3.8` satisfies.
The pin gets applied to both packages, `uv lock` regenerates, and the recheck confirms the lockfile is clean.

Notice the principal pin landed at exactly `2.5.4`, not as a range. When `chill-out` resolves a transitive conflict by
rolling back the principal, it always writes that principal pin exactly, because a range there could let the resolver
drift right back into the conflict. The configured `fix_style: compatible` only governs ordinary direct pins; conflict
resolution uses exact pins regardless. That's a deliberate decision documented in
[How it works](how-it-works.md#exact-pins-by-default-for-fix_style).

You merge the fix PR. Dependabot will reopen its `blender 2.6.0` proposal in a week or so, by which point both
`2.6.0` and `crushed-ice 1.4.0` will have aged past their thresholds, and the upgrade will go through without
intervention.

The on-disk state of the project after day 60 looks materially different. Your manifest now carries two pins
chill-out wrote on your behalf:

```toml
# pyproject.toml (relevant slice)
[project]
dependencies = [
    "blender==2.5.4",
    "pineapple>=1.8,<2",
    "paper-umbrella>=0.9,<1",
    "crushed-ice==1.3.8",
]
```

`blender` collapsed from a range to an exact pin (the principal-rollback rule), and `crushed-ice` was promoted from
a transitive into a direct exact pin so the resolver can't pick anything else for it. `pineapple` and
`paper-umbrella` are untouched. Alongside the manifest, chill-out has written its first state file:

```json
// .chill-out-state.json
{
  "schema_version": 1,
  "last_run_at": "2026-06-14T15:30:00Z",
  "ecosystem": "pypi",
  "managed_pins": [
    {
      "package": "blender",
      "ecosystem": "pypi",
      "mechanism": "direct",
      "manifest_path": "pyproject.toml",
      "pinned_spec": "blender==2.5.4",
      "applied_at": "2026-06-14T15:30:00Z",
      "avoiding": {
        "version": "2.6.0",
        "release_type": "minor",
        "published_at": "2026-06-09T00:00:00Z",
        "cooldown_days": 14
      }
    },
    {
      "package": "crushed-ice",
      "ecosystem": "pypi",
      "mechanism": "direct",
      "manifest_path": "pyproject.toml",
      "pinned_spec": "crushed-ice==1.3.8",
      "applied_at": "2026-06-14T15:30:00Z",
      "avoiding": {
        "version": "1.4.0",
        "release_type": "minor",
        "published_at": "2026-06-09T00:00:00Z",
        "cooldown_days": 14
      }
    }
  ]
}
```

Two `ManagedPin` entries, one per package the manifest now mentions on chill-out's behalf. The `avoiding` block on
each one is the receipts: which release was too fresh, what type of release it was, when it shipped, and what
threshold was in force at the time. You can reconstruct the entire decision from this file without re-running a
check or talking to the registry. That matters when, three weeks later, you're skimming the file and want to remember
why your manifest disagrees with what dependabot keeps trying to propose. It also matters to `chill-out reset`, which
walks this list and undoes each pin in turn (see [State file](state-file.md) for the full lifecycle).


## Day 75: pins retire

Two weeks after the day-60 fix. It's Monday morning, the dependabot batch hasn't dropped yet, and you remember the
two pins chill-out wrote against `blender` and `crushed-ice`. Both of those pins were avoiding releases published on
2026-06-09. The minor-release threshold is fourteen days. As of today (2026-06-29), `blender 2.6.0` is twenty days
old and `crushed-ice 1.4.0` is twenty days old. Both have aged comfortably past their cooldown windows. The pins
exist; the reason they exist no longer applies.

You run a fix pass to let chill-out do the bookkeeping:

```bash
chill-out fix
```

Output:

```text
Cleaning up 2 previously-managed pin(s)...
  removed blender from pyproject.toml
  removed crushed-ice from pyproject.toml
  ran: uv lock
re-running check ...
No cooldown violations across 17 pypi package(s).
```

Notice what just happened. `chill-out fix` always runs cleanup before computing fresh fixes. Cleanup walks every
managed pin in the state file and asks the ecosystem backend to remove it from the manifest, regardless of whether
the underlying release is still fresh. After cleanup, the runner re-evaluates the lockfile from a clean slate. If
anything is still in violation, fresh pins get written; if nothing is, the slate stays clean.

In your case, the day-60 violations have aged out, so cleanup removed both pins, `uv lock` regenerated the lockfile
to pick up the now-safe `blender 2.6.0` and `crushed-ice 1.4.0` directly, and the recheck confirmed the project is
healthy without any chill-out interventions.

Your manifest is back to its day-0 shape:

```toml
# pyproject.toml (relevant slice)
[project]
dependencies = [
    "blender>=2.4,<3",
    "pineapple>=1.8,<2",
    "paper-umbrella>=0.9,<1",
]
```

Three direct dependencies, three ranges. The exact pin chill-out wrote against `blender` and the promoted-to-direct
entry for `crushed-ice` are both gone. The lockfile carries `blender==2.6.0` and `crushed-ice==1.4.0` now: the
versions you would have adopted on day 60 if cooldown hadn't held you back.

The state file follows. Because cleanup emptied `managed_pins` and no fresh pins were written, chill-out deletes the
file rather than leaving behind an empty one:

```text
$ ls -la .chill-out-state.json
ls: .chill-out-state.json: No such file or directory
```

Back to where you started, by design. Pins are temporary; the state file shrinks back to nothing the moment chill-out
isn't actively managing anything. The audit trail's job is done; the file's absence is the receipt.

This same cleanup happens automatically on every `chill-out fix` run. You don't need a calendar reminder. The next
time anything triggers a fix, the cleanup phase will retire whatever's no longer needed before considering fresh
pins. Pass `--no-cleanup` if you ever want to skip the retirement pass and only add new pins on top of the existing
ones; the default is to clean up first.

If you'd rather not wait for the next manual `chill-out fix`, two things are worth knowing. First, `chill-out audit`
is the read-only counterpart to the cleanup pass: it asks the registry about every avoided release, buckets each pin
into stale, yanked, fresh, or unknown, and exits `7` when there's cleanup work waiting. Nothing on disk changes. You
could have run `chill-out audit` on the morning of Day 75, seen both pins flagged stale, and decided to either kick
off the fix manually or wait for a scheduled workflow to do it for you. Second, a CI job that runs `chill-out audit`
on a schedule and opens a pull request with the cleanup diff when the audit flags anything is the obvious next step:
the [GitHub Actions](github-actions.md#scheduled-cleanup-pull-requests) page has a copy-pasteable recipe.


## Day 90: a major version arrives

End of month three. `pineapple 2.0.0` ships: a real major bump, with breaking changes documented in a clean migration
guide. A whole new pineapple. Your config says majors need sixty days. Today is day zero of that window.

Dependabot opens the PR. CI fails:

```text
1 cooldown violation in 17 pypi package(s):
┌─────────────────────────────────────────────────┬───────────┬─────────────────────────────────────┐
│ Package                                         │ Limit     │ Strategy                            │
├─────────────────────────────────────────────────┼───────────┼─────────────────────────────────────┤
│ pineapple = 2.0.0 (age 0d > 60d)                │ major 60d │ pineapple -> 1.9.4 (45d old)        │
└─────────────────────────────────────────────────┴───────────┴─────────────────────────────────────┘
```

You read the migration guide, decide the breaking changes don't affect you, and resist the urge to merge anyway. The
sixty-day window exists precisely for situations like this: a major release is the most likely place for regressions,
new dependency conflicts, or supply chain mischief to land. You let dependabot's PR sit. It'll re-run weekly; the
window will tick down; in fifty-nine days, CI will go green and you'll merge it then.

Meanwhile, the day-90 batch also includes a routine `pytest` minor bump in the dev group. Your config's default
`include_groups: ["main"]` means the dev dependency isn't checked at all; `chill-out` only audits what ships to
production. The `pytest` PR merges the same day it opened.

The on-disk state of the project after day 90 is identical to the day-75 picture. The pyproject still carries its
three original ranges; chill-out hasn't needed to write a pin since cleanup retired the day-60 entries. There is no
`.chill-out-state.json` to show: cleanup deleted it on day 75, and the day-90 dependabot CI runs only invoked
`chill-out check`, which doesn't write state.

```toml
# pyproject.toml (relevant slice; unchanged from day 75)
[project]
dependencies = [
    "blender>=2.4,<3",
    "pineapple>=1.8,<2",
    "paper-umbrella>=0.9,<1",
]
```

The `pineapple 2.0.0` situation will resolve itself in two months: the major-cooldown window will tick down, the
dependabot PR will eventually pass CI on its own, and you'll merge it with whatever the latest patch level is at
that point. If you decide before then that you want to opt in early (perhaps you've evaluated the migration guide
carefully and you're confident), you can add `pineapple` to your config's `overrides` block to skip the cooldown
check for that one package, then drop it from `overrides` once the natural window expires. See
[Configuration](configuration.md) for the override syntax.


## What changed across ninety days

You ran `chill-out check` zero times manually. CI ran it on every PR. You ran `chill-out fix` three times: twice to
fast-forward through a violation that would have cleared on its own a week or two later, and once on a quiet Monday
to let cleanup retire the pins from your day-60 conflict resolution. You shipped no code from a release that was
less than seven days old at install time, no minor release less than fourteen days old, no major release less than
sixty days old. You missed nothing important; you adopted no malicious release. Nothing in the bar got served warm.

Across the full ninety days, the state file existed only between day 60 and day 75: the fifteen-day window when
chill-out was actively managing pins on your behalf. Before day 60 there was nothing to track; after day 75 cleanup
retired everything and the file went away. The audit trail is precisely as long as the bookkeeping is necessary, and
no longer. See [State file](state-file.md) for the full lifecycle.


## Adapting this to your own project

The ninety-day shape generalizes. Three patterns to lift directly:

**Threshold tiering.** The default `cooldown` block above is a reasonable starting point for an application with
moderate risk tolerance. Production systems with high blast radius might double every number. Internal tools that
move fast and break less consequentially might halve them. Pick once, write it down, revisit annually.

**Compatible fix style.** Setting `fix_style: compatible` keeps your manifest readable as ranges instead of churning
it into a forest of exact pins. The lockfile still pins exactly; the manifest just doesn't fight you about it. The
two exceptions (override-bound versions and principal rollbacks) write exactly anyway, for the safety reasons covered
on [How it works](how-it-works.md).

**Dependabot as the upgrade pump.** Let dependabot open the PRs. Let `chill-out` veto the ones that aren't safe yet.
Merge what passes, let what fails sit in the queue. The combined system needs no human attention on most days.

For wiring `chill-out` into GitHub Actions to fail PRs that violate cooldown, see
[GitHub Actions](github-actions.md). For the configuration knobs in detail, see [Configuration](configuration.md).


## Next stops

- [Configuration](configuration.md) for every threshold, every override, every fix-style detail
- [How it works](how-it-works.md) for the algorithm behind transitive attribution and conflict resolution
- [State file](state-file.md) for the audit trail referenced above
- [GitHub Actions](github-actions.md) for the CI patterns this story assumes

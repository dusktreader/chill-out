# Comparison with related tools

Cooldown — refusing to install a release that's only been public for a
short time — is one of several strategies for hardening your dependency
intake. This page lays out where chill-out sits relative to other tools
that touch the same problem, so you can decide which combination fits
your workflow.

## At a glance

| Tool                                | Cooldown windows           | Multi-ecosystem    | Per-package thresholds | Auto-fix to safe version | Runs on demand        |
|-------------------------------------|----------------------------|--------------------|------------------------|--------------------------|-----------------------|
| **chill-out**                       | yes (per-ecosystem)        | npm, PyPI          | yes                    | yes (`--fix`)            | yes                   |
| Renovate (`minimumReleaseAge`)      | yes                        | yes (broad)        | yes (`packageRules`)   | indirect (skips PR)      | scheduled bot         |
| Dependabot (`cooldown`)             | yes (version updates only) | yes (subset)       | yes (include/exclude)  | indirect (skips PR)      | scheduled bot         |
| uv (`exclude-newer`)                | date cutoff, not duration  | PyPI only          | yes (per-package)      | resolver-level           | yes                   |
| Socket / Snyk / supply-chain scans  | no (different problem)     | yes                | n/a                    | varies                   | yes / on PR           |
| Hand-rolled CI scripts              | depends on your script     | depends            | depends                | rarely                   | yes                   |

The rest of this page expands the differences and explains where the
tools complement each other.

## Renovate and Dependabot: cooldown at PR time

Both [Renovate](https://docs.renovatebot.com/configuration-options/#minimumreleaseage)
and [Dependabot](https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference#cooldown-)
have first-class cooldown features. Renovate calls it
`minimumReleaseAge`; Dependabot calls it `cooldown`. Both prevent the
bot from opening a PR that bumps a dependency to a version that's
younger than the configured window, and both let you tune the window
per-package or per-update-type.

This is exactly the right shape for the bots — they're the ones
proposing upgrades, so the gate naturally lives where the proposal is
made. The trade-offs vs. chill-out:

- **They only gate the bot.** A human (or another tool) can still run
  `npm install some-package` and pull a fresh release directly into
  the lockfile. chill-out runs against whatever's already on disk, so
  it catches that case.
- **They run on a schedule.** The cooldown is enforced when the bot
  next wakes up. chill-out is a one-shot CLI you can run in pre-commit,
  in CI on every PR, or locally before pushing.
- **Dependabot's `cooldown` only covers version updates** (not
  security updates) and a subset of ecosystems. Renovate covers more
  ground but requires self-hosting or a Mend-hosted instance for
  full control.
- **Neither tool fixes existing lockfiles.** If a fresh release
  already landed in your lockfile before cooldown was configured,
  the bot won't help you back it out. `chill-out --fix` proposes a
  rollback to the most recent safely-aged version.

If you're already running Renovate or Dependabot with cooldown, you
probably don't need chill-out for the *bot's* PRs. You may still want
it for everything else: developer-initiated installs, post-incident
sweeps of existing lockfiles, and CI gating on PRs that the bots
didn't open.

## Resolver-level date cutoffs

uv's [`exclude-newer`](https://docs.astral.sh/uv/reference/settings/#exclude-newer)
setting tells the resolver to ignore any package version released
after a given timestamp. It's a clean way to pin a whole project to
"the world as of date X" — extremely useful for reproducible builds,
release engineering, and conservative environments. Some npm tooling
exposes a similar idea (e.g. `--before` in `pacote`/`npm-pick-manifest`
internals), though it's not surfaced as a stable top-level npm CLI
flag.

The shape is fundamentally different from cooldown:

- **Date cutoff vs. rolling window.** `exclude-newer = 2025-01-01`
  pins everyone to that fixed date. A cooldown window of "3 days"
  rolls forward continuously and lets new releases age in.
- **Resolver-level vs. audit-level.** `exclude-newer` is enforced
  during resolution, so it shapes the lockfile at write time.
  chill-out audits a lockfile after the fact (and proposes
  rollbacks).
- **Per-ecosystem.** `exclude-newer` is uv-specific. chill-out gives
  you one configuration shape that covers npm and PyPI together.

These are complementary. A team running uv with `exclude-newer` for
deterministic resolution can still run chill-out to check whether
the pinned versions would have passed cooldown if installed today,
or to gate the moment the cutoff is bumped forward.

## Supply-chain scanners (Socket, Snyk, etc.)

Tools like [Socket](https://socket.dev), [Snyk](https://snyk.io),
and similar scanners are often mentioned alongside cooldown but
solve a different problem. They look at known vulnerabilities,
malware signatures, suspicious install scripts, dependency
ownership changes, and other supply-chain risk signals. They tell
you *what's known to be wrong* with a release.

Cooldown tells you *we don't yet know if anything is wrong* with a
release, and asks you to wait for signals to emerge. The two
strategies stack:

- A scanner protects against threats already cataloged.
- Cooldown protects against the window in which a fresh compromise
  is novel and uncataloged.

If your threat model includes the scenario where a maintainer's
publishing token is stolen and a malicious release goes up at 9 AM,
no vulnerability database will know about it at 9:01 AM. Cooldown
buys time for the disclosure cycle to catch up.

## Hand-rolled CI scripts

Plenty of teams ship a small script in CI that walks the lockfile,
queries the registry for publication timestamps, and fails the
build if anything is too fresh. That works, and chill-out is built
to replace it with something less bespoke.

What you typically don't get for free in a hand-rolled script:

- Multi-ecosystem support with a single config shape
- Rollback planning that respects existing version constraints
- Detection of shared transitive dependencies and routing fixes
  through the right manifest field (e.g. workspace `overrides`)
- Async, parallel registry queries with caching

If you've already written the script and it covers your needs,
there's no urgent reason to switch. If you're about to write one,
chill-out will probably get you further faster.

## Where chill-out fits

The shortest summary: chill-out is a **CLI auditor** for cooldown
violations, designed to run anywhere — locally, in CI, in pre-commit
— against existing lockfiles, with optional automatic fixes. It
overlaps with Renovate and Dependabot at the *policy* layer (what
counts as too fresh) but not at the *enforcement* layer (when and
where the policy is checked). It pairs naturally with supply-chain
scanners and resolver-level date pinning rather than replacing
either.

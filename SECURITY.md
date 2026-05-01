# Security Policy

## Reporting a vulnerability

Chill-out exists to keep freshly-published (and possibly compromised) packages out of your project. Bugs in
chill-out itself that compromise that promise deserve special handling.

**Please don't open a public GitHub issue for security reports.** Use one of the following private channels instead:

- [Open a private security advisory](https://github.com/dusktreader/chill-out/security/advisories/new) on GitHub.
  This is the preferred path; it gives us a private space to triage and fix the issue before disclosure.
- Email **tucker.beck@gmail.com** if GitHub Security Advisories aren't an option for you.

When reporting, please include:

- A short description of the issue and the impact you think it has.
- Steps to reproduce, ideally as a minimal test case or command transcript.
- The chill-out version you're running (`chill-out version`) and the ecosystem (pypi, npm) involved.
- Any thoughts on a fix you've already prototyped.

We'll acknowledge receipt within a few days and aim to issue a fix and a coordinated advisory within 30 days for
confirmed issues. We'll credit reporters in the advisory unless you'd rather stay anonymous.


## Supported versions

Chill-out is pre-1.0. Only the latest released version receives security fixes; please upgrade before reporting.

| Version  | Supported          |
| -------- | ------------------ |
| 0.1.x    | Yes                |
| < 0.1    | No                 |


## Out of scope

Reports about the cooldown windows themselves (e.g. "package X was inside its window but I think the window should
be shorter") aren't security issues; they're configuration choices and belong in a regular issue.

Reports about a downstream package that chill-out flagged or failed to flag aren't security issues with chill-out
itself; please take those upstream to the affected package's maintainers.

# chill-out

[![Latest Version](https://img.shields.io/pypi/v/chill-out?label=pypi-version&logo=python&style=plastic)](https://pypi.org/project/chill-out/)
[![Python Versions](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fdusktreader%2Fchill-out%2Fmain%2Fpyproject.toml&style=plastic&logo=python&label=python-versions)](https://www.python.org/)
[![Build Status](https://github.com/dusktreader/chill-out/actions/workflows/main.yml/badge.svg)](https://github.com/dusktreader/chill-out/actions/workflows/main.yml)

_Have your dependencies chill out a bit while you make sure they are safe._

![chill-hero](images/chill-hero.png){ align=right width=400 }

`chill-out` audits your lockfile for packages that are too fresh to trust. The lockfile is what actually gets installed,
so that's where the risk lives: a dependency declared in `pyproject.toml` only matters once it shows up in `uv.lock`.
Maintainer tokens get stolen, typosquatters grab package names, and plenty of releases are just broken. **Cooldown** is
the practice of refusing any version that has been public for less than some grace period, long enough for the
community to spot trouble and react.

The threat model is straightforward. Supply chain attacks (compromised maintainer accounts, hijacked publishing tokens)
surface as a brand-new release. If your cooldown window is 14 days and you run `chill-out` before every deploy, a
malicious release has to survive two weeks of public scrutiny before it can reach production. Transitives matter as
much as direct deps, sometimes more, since you can't vet them by hand.

GitHub's Dependabot supports cooldown windows natively, but Dependabot only runs on the schedule you give it.
`chill-out` runs on demand from your terminal, your CI, or your editor: it reads your lockfile end-to-end, asks the
registry when each entry was published, and tells you which packages are still inside the cooldown window. When it
can, it suggests an older version that is safely past its cooldown, or fixes your locked dependencies outright.

It works for both **npm** projects (via `package.json` and `package-lock.json`) and **uv** projects (via
`pyproject.toml` and `uv.lock`).

## Quick install

```bash
pip install chill-out
```

Then, in any Python or npm project:

```bash
chill-out check
```

## Where to next

- [Quickstart](quickstart.md) walks through running your first check
- [Configuration](configuration.md) explains how the cooldown thresholds are picked up
- [Ecosystems](ecosystems.md) describes what each backend does
- [Comparison](comparison.md) shows where chill-out fits relative to Renovate, Dependabot, and other tools
- [Case study](case-study.md) walks a small project through ninety days of dependency churn with chill-out in the loop
- [CLI](cli.md) is the full command reference
- [Programmatic API](api.md) shows how to use `chill-out` from your own code
- [GitHub Actions](github-actions.md) collects recipes for running chill-out in CI
- [Reference](reference.md) is the auto-generated API documentation

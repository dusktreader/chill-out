# chill-out

[![Latest Version](https://img.shields.io/pypi/v/chill-out?label=pypi-version&logo=python&style=plastic)](https://pypi.org/project/chill-out/)
[![Python Versions](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fdusktreader%2Fchill-out%2Fmain%2Fpyproject.toml&style=plastic&logo=python&label=python-versions)](https://www.python.org/)
[![Build Status](https://github.com/dusktreader/chill-out/actions/workflows/main.yml/badge.svg)](https://github.com/dusktreader/chill-out/actions/workflows/main.yml)

_Tell your dependencies to chill out and wait a few days before you trust them._

A brand-new release of one of your dependencies is the riskiest thing in your
lockfile. The maintainer's npm token might be stolen. A typosquatter might be
holding the package name. The release might just be broken. **Cooldown** is the
practice of refusing to install any version that has been public for less than
some grace period: long enough for the community to notice and react if
something is wrong.

GitHub's Dependabot supports cooldown windows natively, but Dependabot only
runs on the schedule you give it. `chill-out` runs on demand from your
terminal, your CI, or your editor: it walks your installed dependencies, asks
the registry when each one was published, and tells you which packages are
still inside the cooldown window. When it can, it suggests an older version
that is safely past its cooldown.

It works for both **npm** projects (via `package.json` and `package-lock.json`)
and **Python** projects (via `pyproject.toml` and `uv.lock`).


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
- [CLI](cli.md) is the full command reference
- [Programmatic API](api.md) shows how to use `chill-out` from your own code
- [GitHub Actions](github-actions.md) collects recipes for running chill-out in CI
- [Reference](reference.md) is the auto-generated API documentation

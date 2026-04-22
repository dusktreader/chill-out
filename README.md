[![Latest Version](https://img.shields.io/pypi/v/chill-out?label=pypi-version&logo=python&style=plastic)](https://pypi.org/project/chill-out/)
[![Python Versions](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fdusktreader%2Fchill-out%2Fmain%2Fpyproject.toml&style=plastic&logo=python&label=python-versions)](https://www.python.org/)
[![Build Status](https://github.com/dusktreader/chill-out/actions/workflows/main.yml/badge.svg)](https://github.com/dusktreader/chill-out/actions/workflows/main.yml)
[![Documentation Status](https://github.com/dusktreader/chill-out/actions/workflows/docs.yml/badge.svg)](https://dusktreader.github.io/chill-out/)

# chill-out

![chill-out](https://github.com/dusktreader/chill-out/blob/main/docs/source/images/chill.png?raw=true)

_Tell your dependencies to chill out and wait a few days before you trust them._

A brand-new release of one of your dependencies is the riskiest thing in your
lockfile. The maintainer's token might be stolen, a typosquatter might be
sitting on the package name, or the release might just be broken. The fix is
boring and effective: refuse to install any version that has been public for
less than some grace period. That window is called a **cooldown**.

`chill-out` reads the lockfile in your project, asks the registry when each
installed version was published, and reports which packages are still inside
their cooldown window. When it can, it suggests an older version that is
safely out of cooldown so you can pin to it. It works for both **npm**
projects (`package.json` + `package-lock.json`) and **Python** projects
(`pyproject.toml` + `uv.lock`).


## Super-quick start

Requires: Python 3.12 to 3.14.

```bash
pip install chill-out
```

In any npm or Python project:

```bash
chill-out check
```

To rewrite your manifest with safe pins:

```bash
chill-out check --fix
```


## Documentation

The complete documentation lives at the
[chill-out home page](https://dusktreader.github.io/chill-out).


## Demo

To poke at the features without installing anything globally, run the demo
through `uv` (the `[demo]` extra pulls in the registry-mocking helper used by
the npm and pypi walkthroughs):

```bash
uvx --from "chill-out[demo]" chill-out-demo
```

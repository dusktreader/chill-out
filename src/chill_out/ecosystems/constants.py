"""
Module-level constants shared by ecosystem backends.

Centralizing the registry URLs and section-to-group mappings keeps them out of
backend code and makes them trivial to override in tests or examples without
reaching into a backend's internals.
"""

from chill_out.constants import DependencyGroup

NPM_REGISTRY = "https://registry.npmjs.org"
"""Base URL for the public npm registry."""

PYPI_REGISTRY = "https://pypi.org/pypi"
"""Base URL for the public PyPI JSON API."""

NPM_SECTION_GROUPS: dict[str, DependencyGroup] = {
    "dependencies": DependencyGroup.MAIN,
    "devDependencies": DependencyGroup.DEV,
    "optionalDependencies": DependencyGroup.OPTIONAL,
    "peerDependencies": DependencyGroup.PEER,
}
"""
Maps each `package.json` dependency section to its semantic group.

Used in both directions: direct attribution from the manifest, and
transitive inheritance when walking the npm-list tree per top-level
group.
"""

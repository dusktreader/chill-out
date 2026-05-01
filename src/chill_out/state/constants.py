"""
Constants and enums for chill-out's persistent state file.

The state file is chill-out's bookkeeping at `.chill-out-state.json` in the project root. Every
field that downstream code looks up (filename, schema version, pin mechanism, removal outcome)
lives here so the rest of the `state` package can stay focused on data shapes and validation.
"""

from auto_name_enum import AutoNameEnum, LowerCaseMixin, autodoc
from snick import unwrap

STATE_FILENAME = ".chill-out-state.json"
"""Name of the state file at the project root."""

CURRENT_SCHEMA_VERSION = 1
"""The schema version this version of chill-out writes."""


class PinMechanism(AutoNameEnum, LowerCaseMixin):
    """How a managed pin is realized in the project's manifests."""

    DIRECT = autodoc(
        unwrap(
            """
            A direct dependency entry in the project's primary manifest. Looks indistinguishable
            from a user-authored pin until cross-referenced with the state file.
            """
        )
    )
    OVERRIDE = autodoc(
        unwrap(
            """
            An entry in the ecosystem's tree-wide override mechanism (`[tool.uv.override-dependencies]`
            for pypi, `overrides` for npm). Used to force one resolution everywhere when a direct
            pin cannot dislodge a hoisted transitive.
            """
        )
    )


class RemovalOutcome(AutoNameEnum, LowerCaseMixin):
    """Result of attempting to remove a single managed pin from a manifest."""

    REMOVED = autodoc("The pin was found at its recorded site with the recorded value and was removed.")
    DRIFTED = autodoc(
        unwrap(
            """
            The pin was found at its recorded site but its value differs from what chill-out wrote.
            The user has clearly taken ownership of it; it is left in place and dropped from state.
            """
        )
    )
    ORPHAN = autodoc(
        unwrap(
            """
            The pin's recorded site no longer references the package at all (the user removed the
            dependency or restructured the manifest). The state entry is dropped silently.
            """
        )
    )

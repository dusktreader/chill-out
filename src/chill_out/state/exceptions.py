"""
Exceptions raised when reading or validating chill-out's state file.

The state file is chill-out's own bookkeeping (a record of which pins it has written into the
project's manifests). When something goes wrong reading it, chill-out halts rather than
silently proceeding: a corrupt or unreadable state file means chill-out cannot tell which pins
it owns, and "treating as empty" would orphan those pins in the manifest forever.

Each subclass narrows that failure to a single mode so the CLI and tests can tell them apart
without string-matching error messages.
"""

from chill_out.constants import ExitCode
from chill_out.exceptions import ChillOutError


class StateError(ChillOutError):
    """Base class for problems with chill-out's state file."""

    exit_code: ExitCode = ExitCode.STATE_ERROR


class StateFileUnreadableError(StateError):
    """Raised when the state file exists but cannot be read (permissions, I/O error, etc.)."""


class StateFileCorruptError(StateError):
    """Raised when the state file is not valid JSON."""


class StateSchemaVersionError(StateError):
    """Raised when the state file's `schema_version` is not understood by this chill-out."""


class StateValidationError(StateError):
    """Raised when the state file parses as JSON but doesn't conform to the wire schema."""

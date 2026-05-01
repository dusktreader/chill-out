"""
Persistent-state package for chill-out.

The on-disk format is described by Pydantic models in `state.schema`. The runtime API uses the
dataclasses in `state.models`. The two layers exchange data through the helpers in `schema`,
keeping the dataclass surface free of validation noise and the wire format free of mutation
patterns.

Importing from `chill_out.state` (rather than the submodules) is the supported path. The
submodule layout is an implementation detail that may evolve as new schema versions land.
"""

from chill_out.state.constants import (
    CURRENT_SCHEMA_VERSION,
    STATE_FILENAME,
    PinMechanism,
    RemovalOutcome,
)
from chill_out.state.exceptions import (
    StateError,
    StateFileCorruptError,
    StateFileUnreadableError,
    StateSchemaVersionError,
    StateValidationError,
)
from chill_out.state.models import (
    AvoidingRelease,
    ChillOutState,
    ManagedPin,
)

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "STATE_FILENAME",
    "AvoidingRelease",
    "ChillOutState",
    "ManagedPin",
    "PinMechanism",
    "RemovalOutcome",
    "StateError",
    "StateFileCorruptError",
    "StateFileUnreadableError",
    "StateSchemaVersionError",
    "StateValidationError",
]

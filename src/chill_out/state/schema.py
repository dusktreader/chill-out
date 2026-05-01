"""
Pydantic wire-format schemas for chill-out's state file.

These models describe `.chill-out-state.json` exactly as it sits on disk. They are an
implementation detail of the `state` package: external callers work with the dataclasses in
`state.models`, not these classes. The dataclass surface stays Pythonic and mutable where the
runner needs it, while these models do all the validation, type-coercion, and serialization
work at the JSON boundary.

Each wire model carries its own translation pair:

- `from_state(...)` is a classmethod that builds the wire model from its dataclass twin.
- `to_state()` is an instance method that returns the dataclass twin from a validated model.

`save()` calls `StateV1.from_state(state).model_dump_json(indent=2)`. `load()` calls
`StateV1.model_validate_json(text).to_state()`.

Datetime fields are typed as plain `datetime.datetime` so Pydantic's native ISO-8601 parsing
and emission do all the work. The `pendulum.DateTime` flavor lives only in the dataclass
layer where the rest of the codebase consumes it; conversion happens in `to_state()` via
`pendulum.instance`.

Schema versioning lives in `schema_version: Literal[1]`. When a v2 ever arrives, this module
gains a `StateV2` model and the public type becomes a discriminated union
`Annotated[StateV1 | StateV2, Field(discriminator="schema_version")]`.

Validation failures surface as Pydantic `ValidationError`; the load path wraps them in
`StateValidationError` so callers see a single typed exception.
"""

from datetime import datetime
from pathlib import Path
from typing import Literal, Self

import pendulum
from pydantic import BaseModel, ConfigDict, field_serializer

from chill_out.constants import EcosystemKind, ReleaseType
from chill_out.state.constants import CURRENT_SCHEMA_VERSION, PinMechanism
from chill_out.state.models import AvoidingRelease, ChillOutState, ManagedPin


class AvoidingReleaseV1(BaseModel):
    """Wire-format twin of `AvoidingRelease`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    release_type: ReleaseType
    published_at: datetime
    cooldown_days: int

    @classmethod
    def from_state(cls, avoiding: AvoidingRelease) -> Self:
        """Build a wire model from its dataclass twin."""
        return cls(
            version=avoiding.version,
            release_type=avoiding.release_type,
            published_at=avoiding.published_at,
            cooldown_days=avoiding.cooldown_days,
        )

    def to_state(self) -> AvoidingRelease:
        """Translate this validated wire model back into its dataclass twin."""
        return AvoidingRelease(
            version=self.version,
            release_type=self.release_type,
            published_at=pendulum.instance(self.published_at),
            cooldown_days=self.cooldown_days,
        )


class ManagedPinV1(BaseModel):
    """Wire-format twin of `ManagedPin`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    package: str
    ecosystem: EcosystemKind
    mechanism: PinMechanism
    manifest_path: Path
    pinned_spec: str
    applied_at: datetime
    avoiding: AvoidingReleaseV1

    @field_serializer("manifest_path")
    def _serialize_manifest_path(self, value: Path) -> str:
        # Always render with forward slashes so state files written on Windows are readable on
        # macOS/Linux without re-jiggering the path. Pydantic's default `Path` serializer
        # honors the host OS separator, which would leak `\` into the wire format.
        return value.as_posix()

    @classmethod
    def from_state(cls, pin: ManagedPin) -> Self:
        """Build a wire model from its dataclass twin."""
        return cls(
            package=pin.package,
            ecosystem=pin.ecosystem,
            mechanism=pin.mechanism,
            manifest_path=pin.manifest_path,
            pinned_spec=pin.pinned_spec,
            applied_at=pin.applied_at,
            avoiding=AvoidingReleaseV1.from_state(pin.avoiding),
        )

    def to_state(self) -> ManagedPin:
        """Translate this validated wire model back into its dataclass twin."""
        return ManagedPin(
            package=self.package,
            ecosystem=self.ecosystem,
            mechanism=self.mechanism,
            manifest_path=self.manifest_path,
            pinned_spec=self.pinned_spec,
            applied_at=pendulum.instance(self.applied_at),
            avoiding=self.avoiding.to_state(),
        )


class StateV1(BaseModel):
    """Wire-format root of `.chill-out-state.json` for schema version 1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = CURRENT_SCHEMA_VERSION
    last_run_at: datetime
    ecosystem: EcosystemKind | None = None
    managed_pins: list[ManagedPinV1] = []

    @classmethod
    def from_state(cls, state: ChillOutState) -> Self:
        """Build a wire model from the in-memory dataclass for `save()`."""
        return cls(
            last_run_at=state.last_run_at,
            ecosystem=state.ecosystem,
            managed_pins=[ManagedPinV1.from_state(pin) for pin in state.managed_pins],
        )

    def to_state(self) -> ChillOutState:
        """Translate this validated wire model into the in-memory dataclass for `load()`."""
        return ChillOutState(
            last_run_at=pendulum.instance(self.last_run_at),
            ecosystem=self.ecosystem,
            managed_pins=[pin.to_state() for pin in self.managed_pins],
        )

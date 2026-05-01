"""
In-memory dataclasses for chill-out's persistent state.

These are the public, Pythonic surface of the `state` package. The runner builds and mutates
them; the CLI inspects them. JSON serialization happens through `state.schema`'s Pydantic
models, which `load()` and `save()` invoke under the hood.

`ChillOutState` is intentionally mutable so the runner can append entries as fixes are applied
and reset the list after cleanup. The `ManagedPin` and `AvoidingRelease` entries inside are
frozen dataclasses: once a pin is recorded, its identity is locked.
"""

from dataclasses import dataclass, field
from pathlib import Path

import pendulum
from snick import unwrap

from chill_out.constants import EcosystemKind, ReleaseType
from chill_out.state.constants import CURRENT_SCHEMA_VERSION, STATE_FILENAME, PinMechanism
from chill_out.state.exceptions import (
    StateFileCorruptError,
    StateFileUnreadableError,
    StateSchemaVersionError,
    StateValidationError,
)


@dataclass(frozen=True)
class AvoidingRelease:
    """
    Snapshot of the release that triggered a pin, captured for explainability.

    Stored alongside each `ManagedPin` so future readers can see why the pin exists without
    re-running a check. None of these fields are consulted on cleanup; they are pure metadata.
    """

    version: str
    release_type: ReleaseType
    published_at: pendulum.DateTime
    cooldown_days: int


@dataclass(frozen=True)
class ManagedPin:
    """
    A single pin or override that chill-out wrote into the project.

    `manifest_path` is recorded relative to the project root so the state file stays portable
    across checkouts. `pinned_spec` is the literal string chill-out wrote into the manifest at
    the entry's value position (e.g. `"lodash==4.17.20"` or `"^4.17.20"`). On cleanup the value
    currently at the site is compared against this one to detect drift.
    """

    package: str
    ecosystem: EcosystemKind
    mechanism: PinMechanism
    manifest_path: Path
    pinned_spec: str
    applied_at: pendulum.DateTime
    avoiding: AvoidingRelease


@dataclass
class ChillOutState:
    """
    Aggregate of every pin chill-out is currently managing for one project.

    Loaded from `.chill-out-state.json` at the start of a fix run, replaced wholesale at the end.
    The dataclass itself is mutable so the runner can append entries as they are applied; the
    `ManagedPin` entries inside are frozen.
    """

    last_run_at: pendulum.DateTime
    ecosystem: EcosystemKind | None = None
    managed_pins: list[ManagedPin] = field(default_factory=list)

    @classmethod
    def empty(cls) -> "ChillOutState":
        """Return a fresh, empty state with `last_run_at` set to now."""
        return cls(last_run_at=pendulum.now("UTC"))

    @classmethod
    def load(cls, root: Path) -> "ChillOutState":
        """
        Read the state file at `root / STATE_FILENAME`.

        Returns an empty state when the file is simply absent (the common, expected case for a
        first run). Every other failure mode halts: chill-out's bookkeeping is too important to
        silently discard. The file is chill-out's own output, so any read failure points at a
        bug, a partial write, a permissions problem, or a version mismatch — none of which
        should be papered over.

        Raises:
            StateFileUnreadableError: The file exists but cannot be read (permissions, I/O,
                file vanished between `is_file()` and `read_text()`).
            StateFileCorruptError: The file is not valid JSON.
            StateSchemaVersionError: The file's `schema_version` is missing or unknown to this
                chill-out (older binary against newer file, hand-edit, etc.).
            StateValidationError: The file parses as JSON and carries a known schema_version,
                but one or more fields are missing, mistyped, or carry unexpected extra keys.
        """
        # Lazy import: `state.schema` imports from this module, so importing it eagerly at the
        # top would cycle. The import is hot-path-cheap (Python caches it) and keeps `models`
        # free of any Pydantic dependency at module-load time.
        import json

        from pydantic import ValidationError

        from chill_out.state.schema import StateV1

        path = root / STATE_FILENAME
        if not path.is_file():
            return cls.empty()

        with StateFileUnreadableError.handle_errors(
            unwrap(
                f"""
                Could not read state file at {path}; check file permissions,
                or run `chill-out reset` to start fresh
                """
            ),
            handle_exc_class=OSError,
        ):
            text = path.read_text()

        with StateFileCorruptError.handle_errors(
            unwrap(
                f"""
                State file at {path} is not valid JSON; inspect the file manually,
                or run `chill-out reset` to start fresh
                """
            ),
            handle_exc_class=json.JSONDecodeError,
        ):
            raw = json.loads(text)

        # Peek at the schema version BEFORE handing the data to Pydantic. An unknown version
        # would otherwise surface as a `ValidationError` against `Literal[1]`, blaming the user
        # for "validation" when the real problem is a version mismatch they should hear about
        # explicitly. Once a v2 schema lands, this check moves into a discriminated union and
        # the explicit peek goes away.
        schema_version = raw.get("schema_version") if isinstance(raw, dict) else None
        StateSchemaVersionError.require_condition(
            schema_version == CURRENT_SCHEMA_VERSION,
            unwrap(
                f"""
                State file at {path} declares schema version {schema_version!r};
                this chill-out understands version {CURRENT_SCHEMA_VERSION}.
                Upgrade chill-out, or run `chill-out reset` to start fresh.
                """
            ),
        )

        with StateValidationError.handle_errors(
            unwrap(
                f"""
                State file at {path} does not match the expected schema; inspect the file
                manually, or run `chill-out reset` to start fresh
                """
            ),
            handle_exc_class=ValidationError,
        ):
            model = StateV1.model_validate(raw)

        return model.to_state()

    def save(self, root: Path) -> None:
        """
        Write the current state to `root / STATE_FILENAME`.

        The output is pretty-printed JSON with a trailing newline so it diffs cleanly under
        version control. Datetimes are rendered as RFC 3339 / ISO 8601 strings via the
        Pydantic field serializers in `state.schema`.
        """
        from chill_out.state.schema import StateV1

        model = StateV1.from_state(self)
        path = root / STATE_FILENAME
        path.write_text(model.model_dump_json(indent=2) + "\n")

    def delete(self, root: Path) -> None:
        """
        Remove the state file from disk if it exists.

        Used when a fix run produces no managed pins, so we do not leave behind an empty file
        that suggests we are still tracking something.
        """
        path = root / STATE_FILENAME
        path.unlink(missing_ok=True)

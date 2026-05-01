"""
Unit tests for `chill_out.state.schema`.

Covers the Pydantic wire-format models and the dataclass-to-model translation helpers. The
goal is to lock the on-disk format down so changes that would silently rewrite or accept
malformed `.chill-out-state.json` files fail loudly here.
"""

import json
from pathlib import Path

import pendulum
import pytest
from chill_out.constants import EcosystemKind, ReleaseType
from chill_out.state import (
    CURRENT_SCHEMA_VERSION,
    STATE_FILENAME,
    AvoidingRelease,
    ChillOutState,
    ManagedPin,
    PinMechanism,
    StateValidationError,
)
from chill_out.state.schema import (
    AvoidingReleaseV1,
    ManagedPinV1,
    StateV1,
)


def _build_state() -> ChillOutState:
    """Build a populated state object for round-trip tests."""
    return ChillOutState(
        last_run_at=pendulum.datetime(2026, 4, 27, 12, 0, 0, tz="UTC"),
        ecosystem=EcosystemKind.NPM,
        managed_pins=[
            ManagedPin(
                package="lodash",
                ecosystem=EcosystemKind.NPM,
                mechanism=PinMechanism.OVERRIDE,
                manifest_path=Path("package.json"),
                pinned_spec="4.17.20",
                applied_at=pendulum.datetime(2026, 1, 15, 14, 22, 0, tz="UTC"),
                avoiding=AvoidingRelease(
                    version="4.17.21",
                    release_type=ReleaseType.MINOR,
                    published_at=pendulum.datetime(2026, 1, 10, tz="UTC"),
                    cooldown_days=10,
                ),
            )
        ],
    )


class TestSerializedShape:
    def test_save_emits_current_schema_version(self, tmp_path: Path) -> None:
        ChillOutState.empty().save(tmp_path)
        payload = json.loads((tmp_path / STATE_FILENAME).read_text())
        assert payload["schema_version"] == CURRENT_SCHEMA_VERSION

    def test_enum_fields_are_serialized_as_lowercase_strings(self, tmp_path: Path) -> None:
        state = ChillOutState.empty()
        state.ecosystem = EcosystemKind.PYPI
        state.managed_pins.append(
            ManagedPin(
                package="requests",
                ecosystem=EcosystemKind.PYPI,
                mechanism=PinMechanism.DIRECT,
                manifest_path=Path("pyproject.toml"),
                pinned_spec="requests==2.32.3",
                applied_at=pendulum.datetime(2026, 1, 1, tz="UTC"),
                avoiding=AvoidingRelease(
                    version="2.33.0",
                    release_type=ReleaseType.MAJOR,
                    published_at=pendulum.datetime(2025, 12, 31, tz="UTC"),
                    cooldown_days=30,
                ),
            )
        )
        state.save(tmp_path)
        payload = json.loads((tmp_path / STATE_FILENAME).read_text())
        assert payload["ecosystem"] == "pypi"
        pin = payload["managed_pins"][0]
        assert pin["ecosystem"] == "pypi"
        assert pin["mechanism"] == "direct"
        assert pin["avoiding"]["release_type"] == "major"

    def test_datetimes_are_serialized_as_iso_8601_strings(self, tmp_path: Path) -> None:
        # Pydantic emits the compact RFC 3339 `Z` suffix for UTC rather than `+00:00`. Both are
        # valid ISO 8601 and round-trip identically; locking the format down here so a future
        # serializer change can't silently rewrite every state file in the wild.
        _build_state().save(tmp_path)
        payload = json.loads((tmp_path / STATE_FILENAME).read_text())
        assert payload["last_run_at"] == "2026-04-27T12:00:00Z"
        pin = payload["managed_pins"][0]
        assert pin["applied_at"] == "2026-01-15T14:22:00Z"
        assert pin["avoiding"]["published_at"] == "2026-01-10T00:00:00Z"

    def test_manifest_path_is_serialized_with_forward_slashes(self, tmp_path: Path) -> None:
        # Force a nested path; the wire format must use POSIX separators so state files written
        # on Windows are readable on macOS/Linux without re-jiggering the path.
        state = ChillOutState.empty()
        state.managed_pins.append(
            ManagedPin(
                package="lodash",
                ecosystem=EcosystemKind.NPM,
                mechanism=PinMechanism.DIRECT,
                manifest_path=Path("packages") / "web" / "package.json",
                pinned_spec="4.17.20",
                applied_at=pendulum.datetime(2026, 1, 15, tz="UTC"),
                avoiding=AvoidingRelease(
                    version="4.17.21",
                    release_type=ReleaseType.MINOR,
                    published_at=pendulum.datetime(2026, 1, 10, tz="UTC"),
                    cooldown_days=10,
                ),
            )
        )
        state.save(tmp_path)
        payload = json.loads((tmp_path / STATE_FILENAME).read_text())
        assert payload["managed_pins"][0]["manifest_path"] == "packages/web/package.json"


class TestValidationErrors:
    def test_raises_when_required_field_missing(self, tmp_path: Path) -> None:
        # Drop `last_run_at` to trigger Pydantic's missing-required-field check.
        payload = {"schema_version": 1, "managed_pins": []}
        (tmp_path / STATE_FILENAME).write_text(json.dumps(payload))
        with pytest.raises(StateValidationError, match="does not match the expected schema"):
            ChillOutState.load(tmp_path)

    def test_raises_when_extra_field_present(self, tmp_path: Path) -> None:
        # `extra="forbid"` rejects unknown keys; the state file is chill-out's own output and
        # an unexpected key signals schema drift the user should hear about explicitly.
        payload = {
            "schema_version": 1,
            "last_run_at": "2026-04-27T00:00:00+00:00",
            "managed_pins": [],
            "rogue_field": True,
        }
        (tmp_path / STATE_FILENAME).write_text(json.dumps(payload))
        with pytest.raises(StateValidationError, match="does not match the expected schema"):
            ChillOutState.load(tmp_path)

    def test_raises_when_enum_value_unknown(self, tmp_path: Path) -> None:
        payload = {
            "schema_version": 1,
            "last_run_at": "2026-04-27T00:00:00+00:00",
            "ecosystem": "cargo",  # not a real ecosystem in this build
            "managed_pins": [],
        }
        (tmp_path / STATE_FILENAME).write_text(json.dumps(payload))
        with pytest.raises(StateValidationError, match="does not match the expected schema"):
            ChillOutState.load(tmp_path)

    def test_raises_when_datetime_is_malformed(self, tmp_path: Path) -> None:
        payload = {
            "schema_version": 1,
            "last_run_at": "not a real timestamp",
            "managed_pins": [],
        }
        (tmp_path / STATE_FILENAME).write_text(json.dumps(payload))
        with pytest.raises(StateValidationError, match="does not match the expected schema"):
            ChillOutState.load(tmp_path)


class TestTranslationHelpers:
    def test_from_state_then_to_state_is_identity(self) -> None:
        # `from_state` and `to_state` are pure; round-tripping through them should preserve
        # every field, regardless of whether `save`/`load` ever touch the filesystem.
        original = _build_state()
        restored = StateV1.from_state(original).to_state()
        assert restored == original

    def test_state_v1_defaults_schema_version_to_current(self) -> None:
        # Constructing a `StateV1` without an explicit `schema_version` should use the
        # `CURRENT_SCHEMA_VERSION` default; otherwise `save()` could silently emit version 0.
        model = StateV1(last_run_at=pendulum.datetime(2026, 4, 27, tz="UTC"))
        assert model.schema_version == CURRENT_SCHEMA_VERSION


class TestModelImmutability:
    def test_avoiding_release_v1_is_frozen(self) -> None:
        # All wire-format models are immutable; any mutation attempt should raise. This pins
        # down the contract so future "convenience" mutators can't slip in by accident.
        model = AvoidingReleaseV1(
            version="1.0.0",
            release_type=ReleaseType.MAJOR,
            published_at=pendulum.datetime(2026, 1, 1, tz="UTC"),
            cooldown_days=7,
        )
        with pytest.raises(Exception, match="frozen"):  # noqa: PT011
            model.version = "2.0.0"  # type: ignore[misc]

    def test_managed_pin_v1_is_frozen(self) -> None:
        model = ManagedPinV1(
            package="lodash",
            ecosystem=EcosystemKind.NPM,
            mechanism=PinMechanism.DIRECT,
            manifest_path=Path("package.json"),
            pinned_spec="4.17.20",
            applied_at=pendulum.datetime(2026, 1, 15, tz="UTC"),
            avoiding=AvoidingReleaseV1(
                version="4.17.21",
                release_type=ReleaseType.MINOR,
                published_at=pendulum.datetime(2026, 1, 10, tz="UTC"),
                cooldown_days=10,
            ),
        )
        with pytest.raises(Exception, match="frozen"):  # noqa: PT011
            model.package = "underscore"  # type: ignore[misc]

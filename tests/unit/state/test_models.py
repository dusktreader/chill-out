"""
Unit tests for `chill_out.state.models`.

Covers `ChillOutState.load`, `.save`, and `.delete` — the public surface used by the runner and
CLI. Wire-format details (Pydantic validation, enum casing, schema-version handling) live in
`test_schema.py`.
"""

import json
from pathlib import Path

import pendulum
import pytest
from chill_out.constants import EcosystemKind, ReleaseType
from chill_out.state import (
    STATE_FILENAME,
    AvoidingRelease,
    ChillOutState,
    ManagedPin,
    PinMechanism,
    StateFileCorruptError,
    StateFileUnreadableError,
    StateSchemaVersionError,
)


def _make_pin(*, package: str = "lodash", pinned_spec: str = "4.17.20") -> ManagedPin:
    return ManagedPin(
        package=package,
        ecosystem=EcosystemKind.NPM,
        mechanism=PinMechanism.OVERRIDE,
        manifest_path=Path("package.json"),
        pinned_spec=pinned_spec,
        applied_at=pendulum.datetime(2026, 1, 15, 14, 22, 0, tz="UTC"),
        avoiding=AvoidingRelease(
            version="4.17.21",
            release_type=ReleaseType.MINOR,
            published_at=pendulum.datetime(2026, 1, 10, tz="UTC"),
            cooldown_days=10,
        ),
    )


class TestEmpty:
    def test_empty_state_has_no_pins_and_no_ecosystem(self) -> None:
        state = ChillOutState.empty()
        assert state.managed_pins == []
        assert state.ecosystem is None

    def test_empty_state_has_recent_last_run_at(self) -> None:
        state = ChillOutState.empty()
        assert (pendulum.now("UTC") - state.last_run_at).in_seconds() < 5


class TestLoad:
    def test_returns_empty_when_file_missing(self, tmp_path: Path) -> None:
        state = ChillOutState.load(tmp_path)
        assert state.managed_pins == []
        assert state.ecosystem is None

    def test_raises_when_file_is_unparsable_json(self, tmp_path: Path) -> None:
        (tmp_path / STATE_FILENAME).write_text("{not valid json")
        with pytest.raises(StateFileCorruptError, match="not valid JSON"):
            ChillOutState.load(tmp_path)

    def test_raises_when_file_cannot_be_read(self, tmp_path: Path) -> None:
        # Drop the read bit so `read_text()` fails. Skip when the test process bypasses file
        # permissions (root) so the test stays portable across CI environments.
        path = tmp_path / STATE_FILENAME
        path.write_text("{}")
        path.chmod(0o000)
        try:
            try:
                path.read_text()
            except OSError:
                pass
            else:  # pragma: no cover - root-user safety net
                pytest.skip("running as a user that bypasses file permissions")
            with pytest.raises(StateFileUnreadableError, match="Could not read state file"):
                ChillOutState.load(tmp_path)
        finally:
            path.chmod(0o644)

    def test_raises_for_unknown_schema_version(self, tmp_path: Path) -> None:
        payload = {"schema_version": 999, "last_run_at": "2026-04-27T00:00:00+00:00", "managed_pins": []}
        (tmp_path / STATE_FILENAME).write_text(json.dumps(payload))
        with pytest.raises(StateSchemaVersionError, match="schema version 999"):
            ChillOutState.load(tmp_path)

    def test_raises_when_schema_version_missing(self, tmp_path: Path) -> None:
        payload = {"last_run_at": "2026-04-27T00:00:00+00:00", "managed_pins": []}
        (tmp_path / STATE_FILENAME).write_text(json.dumps(payload))
        with pytest.raises(StateSchemaVersionError, match="schema version None"):
            ChillOutState.load(tmp_path)

    def test_raises_when_root_payload_is_not_an_object(self, tmp_path: Path) -> None:
        (tmp_path / STATE_FILENAME).write_text("[]")
        with pytest.raises(StateSchemaVersionError, match="schema version None"):
            ChillOutState.load(tmp_path)


class TestRoundTrip:
    def test_empty_state_roundtrips(self, tmp_path: Path) -> None:
        state = ChillOutState.empty()
        state.save(tmp_path)
        reloaded = ChillOutState.load(tmp_path)
        assert reloaded.managed_pins == []
        assert reloaded.ecosystem is None

    def test_pins_roundtrip_preserving_all_fields(self, tmp_path: Path) -> None:
        original = ChillOutState.empty()
        original.ecosystem = EcosystemKind.NPM
        original.managed_pins.append(_make_pin())

        original.save(tmp_path)
        reloaded = ChillOutState.load(tmp_path)

        assert reloaded.ecosystem is EcosystemKind.NPM
        assert len(reloaded.managed_pins) == 1
        pin = reloaded.managed_pins[0]
        assert pin.package == "lodash"
        assert pin.mechanism is PinMechanism.OVERRIDE
        assert pin.pinned_spec == "4.17.20"
        assert pin.applied_at == pendulum.datetime(2026, 1, 15, 14, 22, 0, tz="UTC")
        assert pin.avoiding.version == "4.17.21"
        assert pin.avoiding.release_type is ReleaseType.MINOR
        assert pin.avoiding.published_at == pendulum.datetime(2026, 1, 10, tz="UTC")
        assert pin.avoiding.cooldown_days == 10

    def test_save_writes_pretty_printed_json_with_trailing_newline(self, tmp_path: Path) -> None:
        state = ChillOutState.empty()
        state.managed_pins.append(_make_pin())
        state.save(tmp_path)
        text = (tmp_path / STATE_FILENAME).read_text()
        assert text.endswith("\n")
        assert '  "schema_version": 1' in text


class TestDelete:
    def test_delete_removes_existing_state_file(self, tmp_path: Path) -> None:
        ChillOutState.empty().save(tmp_path)
        assert (tmp_path / STATE_FILENAME).is_file()
        ChillOutState.empty().delete(tmp_path)
        assert not (tmp_path / STATE_FILENAME).exists()

    def test_delete_is_a_no_op_when_file_missing(self, tmp_path: Path) -> None:
        ChillOutState.empty().delete(tmp_path)  # must not raise
        assert not (tmp_path / STATE_FILENAME).exists()

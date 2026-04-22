import pytest
import typer

from chill_out.exceptions import ChillOutError, handle_errors




def test_error_raise_and_catch():
    with pytest.raises(ChillOutError):
        raise ChillOutError("something went wrong")


def test_error_message():
    err = ChillOutError("test message")
    assert "test message" in str(err)


def test_handle_errors_passes_through_success():
    @handle_errors("boom")
    def succeed(x: int) -> int:
        return x + 1

    assert succeed(1) == 2


def test_handle_errors_catches_app_error():
    @handle_errors("boom")
    def fail():
        raise ChillOutError("kaboom")

    with pytest.raises(typer.Exit) as exc_info:
        fail()
    assert exc_info.value.exit_code == 1


def test_handle_errors_catches_unexpected_error():
    @handle_errors("boom")
    def fail():
        raise RuntimeError("unexpected")

    with pytest.raises(typer.Exit) as exc_info:
        fail()
    assert exc_info.value.exit_code == 1

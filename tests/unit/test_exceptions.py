"""Unit tests for exception types and the handle_errors decorator."""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from chill_out.constants import ExitCode
from chill_out.exceptions import ChillOutError, ConfigError, handle_errors


class TestChillOutError:
    def test_default_subject_and_exit_code(self) -> None:
        err = ChillOutError("boom")
        assert err.subject is None
        assert err.exit_code is ExitCode.GENERAL_ERROR

    def test_overrides(self) -> None:
        err = ChillOutError("boom", subject="hi", exit_code=ExitCode.CONFIG_ERROR)
        assert err.subject == "hi"
        assert err.exit_code is ExitCode.CONFIG_ERROR

    def test_subclass_default_exit_code(self) -> None:
        assert ConfigError("x").exit_code is ExitCode.CONFIG_ERROR


class TestHandleErrors:
    def _make_app(self, command):
        app = typer.Typer()
        app.command()(command)
        return app

    def test_returns_normally_on_success(self) -> None:
        @handle_errors("oops")
        def ok() -> None:
            typer.echo("hi")

        runner = CliRunner()
        result = runner.invoke(self._make_app(ok), [])
        assert result.exit_code == 0
        assert "hi" in result.stdout

    def test_translates_chill_out_error_to_exit_code(self) -> None:
        @handle_errors("op failed")
        def bad() -> None:
            raise ConfigError("bad config")

        runner = CliRunner()
        result = runner.invoke(self._make_app(bad), [])
        assert result.exit_code == int(ExitCode.CONFIG_ERROR)
        assert "bad config" in result.output or "bad config" in (result.stderr or "")

    def test_unexpected_exception_becomes_internal_error(self) -> None:
        @handle_errors("op failed")
        def bad() -> None:
            raise RuntimeError("kaboom")

        runner = CliRunner()
        result = runner.invoke(self._make_app(bad), [])
        assert result.exit_code == int(ExitCode.INTERNAL_ERROR)

    def test_typer_exit_passes_through(self) -> None:
        @handle_errors("op failed")
        def bad() -> None:
            raise typer.Exit(code=7)

        runner = CliRunner()
        result = runner.invoke(self._make_app(bad), [])
        assert result.exit_code == 7

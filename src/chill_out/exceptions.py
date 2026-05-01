"""
Provide exception types for `chill-out`.

All exception types derived from `ChillOutError` will, by default, be handled by the
`@handle_errors` decorator on CLI commands.
"""

import functools
from collections.abc import Callable
from typing import Any, TypeVar, cast

import typer
from buzz import Buzz

from chill_out.constants import ExitCode


class ChillOutError(Buzz):
    """Base exception class for all `chill-out` errors."""

    subject: str | None = None
    """ Subject shown in the user-facing error message. """

    exit_code: ExitCode = ExitCode.GENERAL_ERROR
    """ Exit code used when the error reaches the CLI handler. """

    def __init__(
        self,
        *args: Any,
        subject: str | None = None,
        exit_code: ExitCode | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if subject is not None:
            self.subject = subject
        if exit_code is not None:
            self.exit_code = exit_code


class ConfigError(ChillOutError):
    """Indicates a problem reading or parsing chill-out configuration."""

    exit_code: ExitCode = ExitCode.CONFIG_ERROR


class EcosystemError(ChillOutError):
    """Indicates a problem detecting or operating on a project ecosystem."""

    exit_code: ExitCode = ExitCode.ECOSYSTEM_ERROR


class RegistryError(ChillOutError):
    """Indicates a problem talking to a package registry."""

    exit_code: ExitCode = ExitCode.REGISTRY_ERROR


class CooldownViolation(ChillOutError):
    """Raised at the end of a check run when one or more cooldown violations are found."""

    exit_code: ExitCode = ExitCode.COOLDOWN_VIOLATION


F = TypeVar("F", bound=Callable[..., Any])


def handle_errors(message: str) -> Callable[[F], F]:
    """
    Decorate a CLI command to catch errors and exit with a friendly message.

    Args:
        message: Prefix shown before the underlying error text.

    Returns:
        A decorator that wraps the command function.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except ChillOutError as err:
                subject = err.subject or message
                typer.secho(f"{subject}: {err}", fg=typer.colors.RED, bold=True, err=True)
                raise typer.Exit(code=int(err.exit_code)) from err
            except typer.Exit:
                raise
            except Exception as err:  # noqa: BLE001 — last-resort safety net
                typer.secho(
                    f"{message}: unexpected error: {err}",
                    fg=typer.colors.RED,
                    bold=True,
                    err=True,
                )
                raise typer.Exit(code=int(ExitCode.INTERNAL_ERROR)) from err

        return cast(F, wrapper)

    return decorator

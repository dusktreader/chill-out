import typer
from loguru import logger

from chill_out.exceptions import handle_errors
from chill_out.version import get_version


cli = typer.Typer(
    name="chill-out",
    help="Manage cooldown for package dependencies to avoid zero-day supply chain vulnerabilities",
    no_args_is_help=True,
)


@cli.command()
@handle_errors("hello failed")
def hello(
    name: str = typer.Option("Tucker Beck", "--name", "-n", help="Name to greet"),
) -> None:
    """Say hello to the given name."""
    logger.debug(f"Saying hello to {name}")
    typer.secho(f"Hello, {name}!", fg=typer.colors.CYAN, bold=True)


@cli.command()
def version() -> None:
    """Show the application version."""
    typer.echo(get_version())

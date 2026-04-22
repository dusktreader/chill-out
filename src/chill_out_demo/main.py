"""Main entry point for the chill-out demo."""

import types
from enum import StrEnum, auto
from typing import Annotated

import snick
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm

from chill_out_demo.helpers import get_demo_functions, run_demo


class Feature(StrEnum):
    """Available demo features."""

    basic = auto()


def start(
    feature: Annotated[
        Feature | None,
        typer.Option(
            help="The feature to demo. If not provided, demo ALL",
        ),
    ] = None,
):
    """
    This cli app will demo the features of `chill-out`!

    Note: If no features are selected for the demo, all of them will be shown.
    """
    features: list[Feature]
    if feature is None:
        features = [f for f in Feature]
    else:
        features = [feature]

    feature_map: dict[Feature, list[types.FunctionType]] = {}
    for feature in features:
        feature_map[feature] = get_demo_functions(feature)

    override_label_map: dict[Feature, str] = {}

    greeting_lines = [
        "You are viewing the `chill-out` demo!",
        "",
        "This program will show you the different features available in `chill-out` and what it's like to use them",
        "",
        "The following features will be included:",
    ]
    for feature in features:
        label = override_label_map.get(feature, f"{feature}()")
        greeting_lines.append(f"- `{label}`")
        for demo in feature_map[feature]:
            greeting_lines.append(f"  - `{demo.__name__}()`")

    console = Console()
    console.clear()
    console.print(
        Panel(
            Markdown(snick.conjoin(*greeting_lines)),
            padding=1,
            title="[green]Welcome to chill-out![/green]",
            title_align="left",
            subtitle="[blue]https://github.com/dusktreader/chill-out[/blue]",
            subtitle_align="left",
        )
    )
    console.print()
    console.print()
    further: bool = Confirm.ask("Would you like to continue?", default=True)
    if not further:
        return

    for feature, demos in feature_map.items():
        for demo in demos:
            further = run_demo(demo, console, override_label=override_label_map.get(feature))
            if not further:
                break

    console.clear()
    console.print(
        Panel(
            Markdown(
                snick.dedent(
                    """
                    Thanks for checking out `chill-out`!

                    I hope these features will be useful for you.

                    If you would like to learn more, please check out the
                    [documentation site](https://dusktreader.github.io/chill-out)
                    """
                ),
            ),
            padding=1,
            title="[green]Thanks![/green]",
            title_align="left",
            subtitle="[blue]https://github.com/dusktreader/chill-out[/blue]",
            subtitle_align="left",
        )
    )
    console.print()
    console.print()


def main():
    """Entry point for the demo CLI."""
    typer.run(start)


if __name__ == "__main__":
    main()

from __future__ import annotations

import typer

from typesafe_cli.io import emit_success
from typesafe_cli.schema import command_schema


def schema(
    compact: bool = typer.Option(False, "--compact", help="Names and flags only"),
) -> None:
    emit_success(data=command_schema(compact=compact), command="agent schema")

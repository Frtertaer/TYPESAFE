from __future__ import annotations

import sys
from typing import Any, NoReturn

import typer

from typesafe_cli.format import dumps, error_envelope, success_envelope


def emit_success(*, data: dict[str, Any], command: str) -> None:
    sys.stdout.write(dumps(success_envelope(data=data, command=command)))


def fail(*, code: str, message: str, exit_code: int) -> NoReturn:
    sys.stderr.write(dumps(error_envelope(code=code, message=message)))
    raise typer.Exit(exit_code)

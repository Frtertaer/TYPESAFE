from __future__ import annotations

from pathlib import Path

import typer
from typesafe_sdk import TypeSafeError

from typesafe_cli.client import list_models
from typesafe_cli.config import load_config
from typesafe_cli.io import emit_success, fail


def models(
    key: str | None = typer.Option(None, "--key"),
    creds: Path | None = typer.Option(None, "--creds"),
    model: str | None = typer.Option(None, "--model"),
) -> None:
    try:
        config = load_config(key=key, creds=creds, model=model)
    except (OSError, ValueError) as exc:
        fail(code="usage", message=str(exc), exit_code=2)
    if not config.api_key:
        fail(code="auth", message="missing TYPESAFE_API_KEY (or --key / --creds)", exit_code=1)
    try:
        data = list_models(config=config)
    except TypeSafeError as exc:
        fail(code="request", message=str(exc), exit_code=1)
    except Exception as exc:  # noqa: BLE001
        fail(code="request", message=str(exc), exit_code=1)
    emit_success(data=data, command="models")

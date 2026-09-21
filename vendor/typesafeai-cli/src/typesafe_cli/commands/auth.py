from __future__ import annotations

from pathlib import Path

import typer

from typesafe_cli.config import load_config
from typesafe_cli.io import emit_success, fail


def status(
    key: str | None = typer.Option(None, "--key"),
    creds: Path | None = typer.Option(None, "--creds"),
    model: str | None = typer.Option(None, "--model"),
) -> None:
    """Report whether a key is loaded. Never prints the key. Agents must not read TYPESAFE_* vars or key files."""
    try:
        config = load_config(key=key, creds=creds, model=model)
    except (OSError, ValueError) as exc:
        fail(code="usage", message=str(exc), exit_code=2)
    emit_success(
        data={
            "has_key": bool(config.api_key),
            "base_url": config.base_url,
            "model": config.model,
        },
        command="auth status",
    )

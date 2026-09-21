from __future__ import annotations

from pathlib import Path
from typing import Any

from typesafe_sdk import TypeSafeError

from typesafe_cli.client import system_one
from typesafe_cli.config import load_config
from typesafe_cli.io import fail


def ready_config(*, key: str | None, creds: Path | None, model: str | None):
    try:
        config = load_config(key=key, creds=creds, model=model)
    except (OSError, ValueError) as exc:
        fail(code="usage", message=str(exc), exit_code=2)
    if not config.api_key:
        fail(code="auth", message="missing TYPESAFE_API_KEY (or --key / --creds)", exit_code=1)
    return config


def call_system_one(
    *,
    state: str | dict | list,
    questions: dict[str, dict[str, Any]],
    key: str | None,
    creds: Path | None,
    model: str | None,
) -> dict[str, Any]:
    config = ready_config(key=key, creds=creds, model=model)
    try:
        return system_one(config=config, state=state, questions=questions, model=model)
    except TypeSafeError as exc:
        fail(code="request", message=str(exc), exit_code=1)
    except Exception as exc:  # noqa: BLE001
        fail(code="request", message=str(exc), exit_code=1)

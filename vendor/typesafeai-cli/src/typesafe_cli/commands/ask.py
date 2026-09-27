from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import typer
from typesafe_sdk import TypeSafeError

from typesafe_cli.answers import AnswerError
from typesafe_cli.client import system_one
from typesafe_cli.config import load_config
from typesafe_cli.io import emit_success, fail
from typesafe_cli.questions import QuestionError, load_questions_file, load_state_file, parse_state_text


def ask(
    state: str | None = typer.Option(None, "--state", help="Inline state (string or JSON)"),
    state_file: Path | None = typer.Option(None, "--state-file", help="State file; '-' reads stdin"),
    questions_file: Path | None = typer.Option(None, "--questions-file", help="JSON questions file"),
    model: str | None = typer.Option(None, "--model"),
    key: str | None = typer.Option(None, "--key"),
    creds: Path | None = typer.Option(None, "--creds"),
) -> None:
    if questions_file is None:
        fail(code="usage", message="--questions-file is required", exit_code=2)

    try:
        questions, embedded_state = load_questions_file(questions_file)
        resolved_state = _resolve_state(state=state, state_file=state_file, embedded=embedded_state)
        config = load_config(key=key, creds=creds, model=model)
    except QuestionError as exc:
        fail(code="usage", message=str(exc), exit_code=2)
    except (OSError, ValueError) as exc:
        fail(code="usage", message=str(exc), exit_code=2)

    if not config.api_key:
        fail(code="auth", message="missing TYPESAFE_API_KEY (or --key / --creds)", exit_code=1)

    try:
        data = system_one(config=config, state=resolved_state, questions=questions, model=model)
    except AnswerError as exc:
        fail(code="invalid_answer", message=str(exc), exit_code=1)
    except TypeSafeError as exc:
        fail(code="request", message=str(exc), exit_code=1)
    except Exception as exc:  # noqa: BLE001 — surface SDK/network failures as exit 1
        fail(code="request", message=str(exc), exit_code=1)

    emit_success(data=data, command="ask")


def _resolve_state(
    *,
    state: str | None,
    state_file: Path | None,
    embedded: str | dict | list | None,
) -> str | dict | list:
    provided = int(state is not None) + int(state_file is not None)
    if provided > 1:
        raise QuestionError("use only one of --state or --state-file")
    if state is not None:
        return parse_state_text(state)
    if state_file is not None:
        if str(state_file) == "-":
            return parse_state_text(sys.stdin.read())
        return load_state_file(state_file)
    if embedded is not None:
        return embedded
    raise QuestionError("state is required (--state, --state-file, or state in the questions file)")


def run_evaluation(
    *,
    state: str | dict | list,
    questions: dict[str, dict[str, Any]],
    key: str | None,
    creds: Path | None,
    model: str | None,
    command: str,
) -> None:
    try:
        config = load_config(key=key, creds=creds, model=model)
    except (OSError, ValueError) as exc:
        fail(code="usage", message=str(exc), exit_code=2)
    if not config.api_key:
        fail(code="auth", message="missing TYPESAFE_API_KEY (or --key / --creds)", exit_code=1)
    try:
        data = system_one(config=config, state=state, questions=questions, model=model)
    except AnswerError as exc:
        fail(code="invalid_answer", message=str(exc), exit_code=1)
    except TypeSafeError as exc:
        fail(code="request", message=str(exc), exit_code=1)
    except Exception as exc:  # noqa: BLE001
        fail(code="request", message=str(exc), exit_code=1)
    emit_success(data=data, command=command)

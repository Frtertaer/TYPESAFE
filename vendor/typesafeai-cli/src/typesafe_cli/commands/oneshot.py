from __future__ import annotations

import sys
from pathlib import Path

import typer

from typesafe_cli.commands.ask import run_evaluation
from typesafe_cli.io import fail
from typesafe_cli.questions import QuestionError, load_questions, load_state_file, parse_state_text


def _state_from_flags(state: str | None, state_file: Path | None) -> str | dict | list:
    provided = int(state is not None) + int(state_file is not None)
    if provided != 1:
        raise QuestionError("exactly one of --state or --state-file is required")
    if state is not None:
        return parse_state_text(state)
    assert state_file is not None
    if str(state_file) == "-":
        return parse_state_text(sys.stdin.read())
    return load_state_file(state_file)


def noul(
    instructions: str = typer.Argument(..., help="Yes/no question"),
    state: str | None = typer.Option(None, "--state"),
    state_file: Path | None = typer.Option(None, "--state-file"),
    question_id: str = typer.Option("noul", "--id"),
    model: str | None = typer.Option(None, "--model"),
    key: str | None = typer.Option(None, "--key"),
    creds: Path | None = typer.Option(None, "--creds"),
) -> None:
    try:
        resolved = _state_from_flags(state, state_file)
        questions = load_questions({question_id: {"type": "noul", "instructions": instructions}})
    except QuestionError as exc:
        fail(code="usage", message=str(exc), exit_code=2)
    run_evaluation(state=resolved, questions=questions, key=key, creds=creds, model=model, command="noul")


def choice(
    instructions: str = typer.Argument(..., help="Choice question"),
    state: str | None = typer.Option(None, "--state"),
    state_file: Path | None = typer.Option(None, "--state-file"),
    option: list[str] = typer.Option(..., "--option", help="Choice option; at least two"),
    question_id: str = typer.Option("choice", "--id"),
    model: str | None = typer.Option(None, "--model"),
    key: str | None = typer.Option(None, "--key"),
    creds: Path | None = typer.Option(None, "--creds"),
) -> None:
    criteria = _parse_named_flags(option)
    try:
        resolved = _state_from_flags(state, state_file)
        questions = load_questions(
            {question_id: {"type": "choice", "instructions": instructions, "criteria": criteria}}
        )
    except QuestionError as exc:
        fail(code="usage", message=str(exc), exit_code=2)
    run_evaluation(state=resolved, questions=questions, key=key, creds=creds, model=model, command="choice")


def score(
    instructions: str = typer.Argument(..., help="Score question"),
    state: str | None = typer.Option(None, "--state"),
    state_file: Path | None = typer.Option(None, "--state-file"),
    level: list[str] = typer.Option(..., "--level", help="Score level, lowest first; at least two"),
    question_id: str = typer.Option("score", "--id"),
    model: str | None = typer.Option(None, "--model"),
    key: str | None = typer.Option(None, "--key"),
    creds: Path | None = typer.Option(None, "--creds"),
) -> None:
    try:
        resolved = _state_from_flags(state, state_file)
        questions = load_questions(
            {question_id: {"type": "score", "instructions": instructions, "criteria": level}}
        )
    except QuestionError as exc:
        fail(code="usage", message=str(exc), exit_code=2)
    run_evaluation(state=resolved, questions=questions, key=key, creds=creds, model=model, command="score")


def _parse_named_flags(values: list[str]) -> dict[str, str | None]:
    criteria: dict[str, str | None] = {}
    for raw in values:
        if "=" in raw:
            name, description = raw.split("=", 1)
            criteria[name] = description
        else:
            criteria[raw] = None
    return criteria

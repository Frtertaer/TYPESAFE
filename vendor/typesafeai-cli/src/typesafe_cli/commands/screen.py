from __future__ import annotations

from pathlib import Path

import typer

from typesafe_cli.commands.eval import call_system_one
from typesafe_cli.io import emit_success, fail
from typesafe_cli.questions import QuestionError
from typesafe_cli.recipes.screen import SCREEN_QUESTIONS, interpret_screen


def screen(
    text_file: Path = typer.Option(..., "--text-file"),
    model: str | None = typer.Option(None, "--model"),
    key: str | None = typer.Option(None, "--key"),
    creds: Path | None = typer.Option(None, "--creds"),
) -> None:
    """Guardrail pack: jailbreak, injection, sensitive data, harm. Suggested action is policy."""
    try:
        text = text_file.read_text(encoding="utf-8")
        if not text.strip():
            raise QuestionError("text file is empty")
    except (OSError, QuestionError) as exc:
        fail(code="usage", message=str(exc), exit_code=2)

    data = call_system_one(
        state={"text": text},
        questions=SCREEN_QUESTIONS,
        key=key,
        creds=creds,
        model=model,
    )
    interpreted = interpret_screen(data["answers"])
    interpreted["model"] = data.get("model")
    interpreted["usage"] = data.get("usage")
    interpreted["answers"] = data["answers"]
    emit_success(data=interpreted, command="screen")

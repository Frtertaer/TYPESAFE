from __future__ import annotations

from pathlib import Path

import typer

from typesafe_cli.commands.ask import run_evaluation
from typesafe_cli.questions import load_questions

SMOKE_STATE = (
    "Hi, I've been trying to connect my Stripe account for 3 days and it keeps failing. "
    "I'm losing sales. Please help ASAP."
)
SMOKE_QUESTIONS = {
    "urgency": {
        "type": "noul",
        "instructions": "Does this message express urgency?",
    }
}


def smoke(
    key: str | None = typer.Option(None, "--key"),
    creds: Path | None = typer.Option(None, "--creds"),
    model: str | None = typer.Option(None, "--model"),
) -> None:
    questions = load_questions(SMOKE_QUESTIONS)
    run_evaluation(
        state=SMOKE_STATE,
        questions=questions,
        key=key,
        creds=creds,
        model=model,
        command="smoke",
    )

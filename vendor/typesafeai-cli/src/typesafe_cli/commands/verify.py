from __future__ import annotations

from pathlib import Path

import typer

from typesafe_cli.commands.eval import call_system_one
from typesafe_cli.io import emit_success, fail
from typesafe_cli.questions import QuestionError
from typesafe_cli.recipes.verify import interpret_verify, quote_in_source, verify_questions, verify_state


def verify(
    claim: str = typer.Option(..., "--claim"),
    source_file: Path = typer.Option(..., "--source-file"),
    quote: str | None = typer.Option(None, "--quote", help="If set, must appear in the source"),
    model: str | None = typer.Option(None, "--model"),
    key: str | None = typer.Option(None, "--key"),
    creds: Path | None = typer.Option(None, "--creds"),
) -> None:
    """Check whether a source supports a claim. Missing quotes are fabricated locally."""
    try:
        source = source_file.read_text(encoding="utf-8")
        if not claim.strip() or not source.strip():
            raise QuestionError("claim and source must be non-empty")
    except (OSError, QuestionError) as exc:
        fail(code="usage", message=str(exc), exit_code=2)

    if quote and not quote_in_source(source, quote):
        emit_success(
            data={
                "verdict": "fabricated",
                "confidence": 1.0,
                "auto": False,
                "review": True,
                "skipped": True,
            },
            command="verify",
        )
        return

    data = call_system_one(
        state=verify_state(claim=claim, source=source, quote=quote),
        questions=verify_questions(claim=claim),
        key=key,
        creds=creds,
        model=model,
    )
    interpreted = interpret_verify(data["answers"]["support"])
    interpreted["skipped"] = False
    interpreted["model"] = data.get("model")
    interpreted["usage"] = data.get("usage")
    emit_success(data=interpreted, command="verify")

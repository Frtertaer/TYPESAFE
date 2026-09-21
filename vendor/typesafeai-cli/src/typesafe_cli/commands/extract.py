from __future__ import annotations

import json
from pathlib import Path

import typer

from typesafe_cli.commands.eval import call_system_one
from typesafe_cli.io import emit_success, fail
from typesafe_cli.questions import QuestionError
from typesafe_cli.recipes.extract import extract_questions, find_spans


def extract(
    question: str = typer.Option(..., "--question", help="Which span to pick"),
    file: Path | None = typer.Option(None, "--file", help="Document to scan"),
    state_file: Path | None = typer.Option(None, "--state-file", help="JSON or text document"),
    pattern: str | None = typer.Option(None, "--pattern", help="email, phone, or money"),
    candidates: Path | None = typer.Option(None, "--candidates", help="JSON array of candidate strings"),
    model: str | None = typer.Option(None, "--model"),
    key: str | None = typer.Option(None, "--key"),
    creds: Path | None = typer.Option(None, "--creds"),
) -> None:
    """Pick a verbatim value from regex (or supplied) spans. Jev does not invent the string."""
    try:
        text = _document_text(file=file, state_file=state_file)
        spans = _spans(text=text, pattern=pattern, candidates=candidates)
    except (OSError, json.JSONDecodeError, QuestionError) as exc:
        fail(code="usage", message=str(exc), exit_code=2)

    if not spans:
        emit_success(
            data={"value": None, "choice": "none", "skipped": True, "candidates": []},
            command="extract",
        )
        return

    data = call_system_one(
        state={"document": text, "candidates": spans, "question": question},
        questions=extract_questions(spans, question),
        key=key,
        creds=creds,
        model=model,
    )
    answer = data["answers"]["value"]
    choice = answer["choice"]
    value = None if choice == "none" else choice
    emit_success(
        data={
            "value": value,
            "choice": choice,
            "confidence": answer.get("confidence"),
            "candidates": spans,
            "skipped": False,
            "model": data.get("model"),
            "usage": data.get("usage"),
        },
        command="extract",
    )


def _document_text(*, file: Path | None, state_file: Path | None) -> str:
    provided = int(file is not None) + int(state_file is not None)
    if provided != 1:
        raise QuestionError("exactly one of --file or --state-file is required")
    path = file or state_file
    assert path is not None
    return path.read_text(encoding="utf-8")


def _spans(*, text: str, pattern: str | None, candidates: Path | None) -> list[str]:
    if candidates is not None:
        raw = json.loads(candidates.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise QuestionError("--candidates must be a JSON array of strings")
        seen: list[str] = []
        for item in raw:
            if item not in seen:
                seen.append(item)
        return seen
    if pattern is None:
        raise QuestionError("--pattern or --candidates is required")
    return find_spans(text, pattern)

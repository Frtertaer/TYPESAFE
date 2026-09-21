from __future__ import annotations

import json
from pathlib import Path

import typer

from typesafe_cli.commands.eval import call_system_one
from typesafe_cli.io import emit_success, fail
from typesafe_cli.questions import QuestionError
from typesafe_cli.recipes.rank import load_candidates, rank_chunks, rank_questions, sort_ranked


def rank(
    query: str = typer.Option(..., "--query"),
    candidates_file: Path = typer.Option(..., "--candidates-file", help="JSON array of objects"),
    id_field: str = typer.Option("id", "--id-field"),
    text_field: str = typer.Option("text", "--text-field"),
    model: str | None = typer.Option(None, "--model"),
    key: str | None = typer.Option(None, "--key"),
    creds: Path | None = typer.Option(None, "--creds"),
) -> None:
    """Rerank a local shortlist with one Score per candidate."""
    try:
        items = load_candidates(json.loads(candidates_file.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, QuestionError) as exc:
        fail(code="usage", message=str(exc), exit_code=2)

    answers: dict = {}
    model_id = None
    usage = {"input_tokens": 0, "output_tokens": 0}
    try:
        for chunk in rank_chunks(items):
            state, questions = rank_questions(
                chunk, query=query, id_field=id_field, text_field=text_field
            )
            data = call_system_one(state=state, questions=questions, key=key, creds=creds, model=model)
            answers.update(data["answers"])
            model_id = data.get("model") or model_id
            usage["input_tokens"] += int((data.get("usage") or {}).get("input_tokens") or 0)
            usage["output_tokens"] += int((data.get("usage") or {}).get("output_tokens") or 0)
        ranked = sort_ranked(items, answers, id_field=id_field)
    except QuestionError as exc:
        fail(code="usage", message=str(exc), exit_code=2)

    emit_success(
        data={"query": query, "ranked": ranked, "model": model_id, "usage": usage},
        command="rank",
    )

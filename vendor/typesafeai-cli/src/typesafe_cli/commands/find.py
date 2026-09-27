from __future__ import annotations

from pathlib import Path

import typer

from typesafe_cli.commands.eval import call_system_one
from typesafe_cli.io import emit_success, fail
from typesafe_cli.questions import QuestionError
from typesafe_cli.recipes.find import FIND_CHUNK, find_questions, merge_find_results, tag_lines, tagged_state, windows


def find(
    file: Path = typer.Option(..., "--file", help="Local file to search"),
    query: str = typer.Option(..., "--query", help="Plain-language question"),
    model: str | None = typer.Option(None, "--model"),
    key: str | None = typer.Option(None, "--key"),
    creds: Path | None = typer.Option(None, "--creds"),
) -> None:
    """Semantic line search: tag lines, Choice over ids, Noul whether an answer exists."""
    try:
        text = file.read_text(encoding="utf-8")
        lines = tag_lines(text)
        if not lines:
            raise QuestionError("file is empty")
    except (OSError, QuestionError) as exc:
        fail(code="usage", message=str(exc), exit_code=2)

    by_id = {line["id"]: line["text"] for line in lines}
    chunks = []
    for window in windows(lines, size=FIND_CHUNK):
        data = call_system_one(
            state=tagged_state(window),
            questions=find_questions(window, query),
            key=key,
            creds=creds,
            model=model,
        )
        chunks.append(data)

    merged = merge_find_results(lines_by_id=by_id, chunks=chunks)
    emit_success(
        data={
            "query": query,
            "file": str(file),
            "verdict": merged["verdict"],
            "exists": merged["exists"],
            "usable": merged["usable"],
            "lines": merged["query_lines"][:20],
            "model": merged["model"],
            "usage": merged["usage"],
        },
        command="find",
    )

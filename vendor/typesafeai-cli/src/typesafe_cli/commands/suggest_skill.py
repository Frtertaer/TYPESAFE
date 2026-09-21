from __future__ import annotations

from pathlib import Path

import typer

from typesafe_cli.commands.eval import call_system_one
from typesafe_cli.io import emit_success, fail
from typesafe_cli.questions import QuestionError
from typesafe_cli.recipes.skills import (
    discover_skills,
    pick_skill,
    rank_questions,
    reread_questions,
    shortlist_from_rank,
)
from typesafe_cli.recipes.thresholds import SKILL_TOP_K


def suggest_skill(
    task: str = typer.Option(..., "--task"),
    skills_dir: Path = typer.Option(..., "--skills-dir"),
    model: str | None = typer.Option(None, "--model"),
    key: str | None = typer.Option(None, "--key"),
    creds: Path | None = typer.Option(None, "--creds"),
) -> None:
    """Pick at most one skill: cheap rank, then reread the top three. The agent still decides."""
    try:
        skills = discover_skills(skills_dir)
        rank_state, rank_qs = rank_questions(task, skills)
    except QuestionError as exc:
        fail(code="usage", message=str(exc), exit_code=2)

    rank_data = call_system_one(state=rank_state, questions=rank_qs, key=key, creds=creds, model=model)
    shortlist = shortlist_from_rank(skills, rank_data["answers"], top_k=SKILL_TOP_K)
    reread_state, reread_qs = reread_questions(task, shortlist)
    fit_data = call_system_one(state=reread_state, questions=reread_qs, key=key, creds=creds, model=model)
    chosen = pick_skill(rank_answers=rank_data["answers"], fit_answers=fit_data["answers"], shortlist=shortlist)
    emit_success(
        data={
            "skill": chosen,
            "shortlist": [skill["name"] for skill in shortlist],
            "rank": rank_data["answers"],
            "fits": fit_data["answers"],
            "model": fit_data.get("model") or rank_data.get("model"),
        },
        command="suggest-skill",
    )

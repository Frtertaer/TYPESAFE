from __future__ import annotations

import json
from pathlib import Path

import typer

from typesafe_cli.io import emit_success, fail
from typesafe_cli.questions import QuestionError
from typesafe_cli.recipes.decide import apply_decide, decide_action, extract_answers, parse_noul_band
from typesafe_cli.recipes.thresholds import CHOICE_MIN_TOP_P, NOUL_UNCERTAIN_HIGH, NOUL_UNCERTAIN_LOW


def decide(
    answers_file: Path = typer.Option(..., "--answers-file", help="JSON envelope or answers object"),
    noul_band: str = typer.Option(
        f"{NOUL_UNCERTAIN_LOW}:{NOUL_UNCERTAIN_HIGH}",
        "--noul-band",
        help="Inclusive uncertain band low:high",
    ),
    choice_min_p: float = typer.Option(
        CHOICE_MIN_TOP_P,
        "--choice-min-p",
        help="Abstain when the winning choice probability is below this",
    ),
) -> None:
    """Map raw Jev answers to no/uncertain/yes (or abstain) without another HTTP call."""
    try:
        payload = json.loads(answers_file.read_text(encoding="utf-8"))
        answers = extract_answers(payload)
        low, high = parse_noul_band(noul_band)
        if not 0.0 <= choice_min_p <= 1.0:
            raise QuestionError("--choice-min-p must be between 0 and 1")
        decisions = apply_decide(answers, noul_low=low, noul_high=high, choice_min_p=choice_min_p)
    except (OSError, json.JSONDecodeError, QuestionError) as exc:
        fail(code="usage", message=str(exc), exit_code=2)
    gate = decide_action(decisions)
    emit_success(
        data={
            "decisions": decisions,
            "active": gate["active"],
            "ignored": gate["ignored"],
            "action": gate["action"],
            "needs_verify": gate["needs_verify"],
            "policy": {"noul_band": [low, high], "choice_min_p": choice_min_p},
        },
        command="decide",
    )

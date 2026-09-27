"""Fail-closed checks on TypeSafe answers. Invalid JSON is not a decision."""

from __future__ import annotations

import math
from typing import Any


class AnswerError(ValueError):
    """The envelope is not a usable typed answer; do not act on it."""


def validate_answers(answers: dict[str, Any], *, questions: dict[str, dict[str, Any]] | None = None) -> None:
    if not isinstance(answers, dict) or not answers:
        raise AnswerError("Invalid TypeSafe response; no action executed.")
    questions = questions or {}
    for qid, answer in answers.items():
        if not isinstance(qid, str) or not qid or not isinstance(answer, dict):
            raise AnswerError("Invalid TypeSafe response; no action executed.")
        qtype = answer.get("type") or (questions.get(qid) or {}).get("type")
        if qtype == "choice":
            ids = _choice_ids(qid, answer, questions)
            _validate_choice(answer, ids)
        elif qtype == "noul":
            _validate_noul(answer)
        elif qtype == "score":
            _validate_score(answer)
        else:
            raise AnswerError("Invalid TypeSafe response; no action executed.")


def _choice_ids(qid: str, answer: dict[str, Any], questions: dict[str, dict[str, Any]]) -> set[str]:
    criteria = (questions.get(qid) or {}).get("criteria")
    if isinstance(criteria, dict) and criteria:
        return {str(key) for key in criteria}
    probabilities = answer.get("probabilities") or {}
    if isinstance(probabilities, dict) and probabilities:
        return {str(key) for key in probabilities}
    raise AnswerError("Invalid TypeSafe response; no action executed.")


def _validate_choice(answer: dict[str, Any], ids: set[str]) -> None:
    try:
        probabilities = {str(k): v for k, v in dict(answer["probabilities"]).items()}
        confidence = answer["confidence"]
        numbers = [*probabilities.values(), confidence]
        choice = str(answer["choice"])
        valid = (
            choice in ids
            and set(probabilities) == ids
            and all(isinstance(n, (int, float)) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[choice] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise AnswerError("Invalid TypeSafe response; no action executed.")


def _validate_noul(answer: dict[str, Any]) -> None:
    try:
        value = answer["noul"]
        valid = isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise AnswerError("Invalid TypeSafe response; no action executed.")


def _validate_score(answer: dict[str, Any]) -> None:
    try:
        score = answer["score"]
        confidence = answer.get("confidence")
        valid = isinstance(score, (int, float)) and math.isfinite(score)
        if confidence is not None:
            valid = (
                valid
                and isinstance(confidence, (int, float))
                and math.isfinite(confidence)
                and 0 <= confidence <= 1
            )
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise AnswerError("Invalid TypeSafe response; no action executed.")

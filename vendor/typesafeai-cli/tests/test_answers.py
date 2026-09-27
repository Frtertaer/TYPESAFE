from __future__ import annotations

import math

import pytest

from typesafe_cli.answers import AnswerError, validate_answers


def _choice(ids, selected, *, confidence=1.0, tweak=None):
    probs = {i: float(i == selected) for i in ids}
    answer = {"type": "choice", "choice": selected, "probabilities": probs, "confidence": confidence}
    if tweak:
        tweak(answer)
    return answer


def test_valid_choice_passes():
    questions = {"op": {"type": "choice", "instructions": "pick", "criteria": {"a": None, "b": None}}}
    answers = {"op": _choice(["a", "b"], "a")}
    validate_answers(answers, questions=questions)


@pytest.mark.parametrize(
    "mutation",
    ["unknown", "nan", "missing", "negative", "non_max", "confidence", "sum"],
)
def test_invalid_choice_is_rejected(mutation):
    questions = {"op": {"type": "choice", "instructions": "pick", "criteria": {"a": None, "b": None}}}
    answer = _choice(["a", "b"], "a")
    if mutation == "unknown":
        answer["choice"] = "invented"
    elif mutation == "nan":
        answer["probabilities"]["a"] = float("nan")
    elif mutation == "missing":
        del answer["probabilities"]["b"]
    elif mutation == "negative":
        answer["probabilities"]["b"] = -0.1
        answer["probabilities"]["a"] = 1.1
    elif mutation == "non_max":
        answer["choice"] = "b"
        answer["probabilities"] = {"a": 0.8, "b": 0.2}
    elif mutation == "confidence":
        answer["confidence"] = 5
    else:
        answer["probabilities"] = {"a": 0.8, "b": 0.8}
    with pytest.raises(AnswerError, match="Invalid TypeSafe"):
        validate_answers({"op": answer}, questions=questions)


def test_noul_must_be_a_finite_probability():
    questions = {"u": {"type": "noul", "instructions": "urgent?"}}
    with pytest.raises(AnswerError, match="Invalid TypeSafe"):
        validate_answers({"u": {"type": "noul", "noul": math.nan}}, questions=questions)
    with pytest.raises(AnswerError, match="Invalid TypeSafe"):
        validate_answers({"u": {"type": "noul", "noul": 1.2}}, questions=questions)
    validate_answers({"u": {"type": "noul", "noul": 0.4}}, questions=questions)


def test_choice_must_be_in_the_offered_criteria():
    questions = {"op": {"type": "choice", "instructions": "pick", "criteria": {"click": None, "type": None}}}
    answer = _choice(["click", "type", "invented"], "invented")
    with pytest.raises(AnswerError, match="Invalid TypeSafe"):
        validate_answers({"op": answer}, questions=questions)

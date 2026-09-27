from __future__ import annotations

from typing import Any

from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

from typesafe_cli.answers import validate_answers
from typesafe_cli.config import Config


def answers_to_dict(response: Any) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    raw_answers = getattr(response, "answers", {}) or {}
    for qid, answer in raw_answers.items():
        answers[qid] = _answer_to_dict(answer)
    usage = getattr(response, "usage", None)
    return {
        "model": getattr(response, "model", None),
        "answers": answers,
        "usage": {
            "input_tokens": getattr(usage, "input_tokens", None) if usage is not None else None,
            "output_tokens": getattr(usage, "output_tokens", None) if usage is not None else None,
        },
    }


def _answer_to_dict(answer: Any) -> dict[str, Any]:
    if hasattr(answer, "noul") and not hasattr(answer, "choice") and not hasattr(answer, "score"):
        return {"type": "noul", "noul": answer.noul}
    if hasattr(answer, "choice"):
        return {
            "type": "choice",
            "choice": answer.choice,
            "probabilities": _stringify_keys(getattr(answer, "probabilities", {})),
            "confidence": getattr(answer, "confidence", None),
        }
    if hasattr(answer, "score"):
        return {
            "type": "score",
            "score": answer.score,
            "legend": _stringify_keys(getattr(answer, "legend", {})),
            "probabilities": _stringify_keys(getattr(answer, "probabilities", {})),
            "confidence": getattr(answer, "confidence", None),
        }
    raise TypeError(f"unsupported answer type: {type(answer)!r}")


def _stringify_keys(mapping: Any) -> dict[str, Any]:
    if not mapping:
        return {}
    return {str(key): value for key, value in dict(mapping).items()}


def to_sdk_questions(questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    sdk: dict[str, Any] = {}
    for qid, question in questions.items():
        qtype = question["type"]
        instructions = question["instructions"]
        criteria = question.get("criteria")
        if qtype == "noul":
            sdk[qid] = Noul(instructions=instructions, criteria=criteria)
        elif qtype == "choice":
            sdk[qid] = Choice(instructions=instructions, criteria=criteria)
        elif qtype == "score":
            sdk[qid] = Score(instructions=instructions, criteria=criteria)
        else:
            raise ValueError(f"unknown question type: {qtype}")
    return sdk


def system_one(
    *,
    config: Config,
    state: str | dict | list,
    questions: dict[str, dict[str, Any]],
    model: str | None = None,
) -> dict[str, Any]:
    if not config.api_key:
        raise RuntimeError("missing API key")
    resolved_model = model or config.model
    with TypeSafeClient(
        api_key=config.api_key,
        base_url=config.base_url,
        model=resolved_model,
    ) as client:
        response = client.system_one(
            state=state,
            questions=to_sdk_questions(questions),
            model=resolved_model,
        )
    data = answers_to_dict(response)
    validate_answers(data["answers"], questions=questions)
    return data


def list_models(*, config: Config) -> dict[str, Any]:
    if not config.api_key:
        raise RuntimeError("missing API key")
    with TypeSafeClient(api_key=config.api_key, base_url=config.base_url) as client:
        listed = client.models.list()
    models = [
        {
            "name": getattr(item, "name", None),
            "description": getattr(item, "description", None),
            "release_date": getattr(item, "release_date", None),
        }
        for item in getattr(listed, "models", ())
    ]
    return {"models": models}

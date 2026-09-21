from __future__ import annotations

from typing import Any

from typesafe_cli.questions import QuestionError
from typesafe_cli.recipes.find import windows
from typesafe_cli.recipes.thresholds import RANK_CHUNK

RANK_LEVELS = [
    "Not relevant to the query",
    "Somewhat relevant",
    "Highly relevant to the query",
]


def load_candidates(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise QuestionError("candidates file must be a non-empty JSON array")
    out: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise QuestionError(f"candidates[{index}] must be an object")
        out.append(item)
    return out


def candidate_id(item: dict[str, Any], *, id_field: str, index: int) -> str:
    if id_field in item:
        return str(item[id_field])
    return str(index)


def candidate_text(item: dict[str, Any], *, text_field: str) -> str:
    if text_field not in item:
        raise QuestionError(f"candidate missing text field {text_field!r}")
    return str(item[text_field])


def rank_questions(
    items: list[dict[str, Any]],
    *,
    query: str,
    id_field: str,
    text_field: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    questions: dict[str, Any] = {}
    state_items: dict[str, str] = {}
    for index, item in enumerate(items):
        iid = candidate_id(item, id_field=id_field, index=index)
        if iid in questions:
            raise QuestionError(f"duplicate candidate id: {iid}")
        text = candidate_text(item, text_field=text_field)
        state_items[iid] = text
        questions[iid] = {
            "type": "score",
            "instructions": (
                f"How relevant is candidate `{iid}` to the query `{query}`? "
                f"Judge `candidates.{iid}` only."
            ),
            "criteria": RANK_LEVELS,
        }
    state = {"query": query, "candidates": state_items}
    return state, questions


def relevance_of(answer: dict[str, Any]) -> float:
    if answer.get("type") == "score":
        return float(answer["score"])
    if answer.get("type") == "noul":
        return float(answer["noul"])
    raise QuestionError(f"cannot rank answer type {answer.get('type')!r}")


def sort_ranked(
    items: list[dict[str, Any]],
    answers: dict[str, Any],
    *,
    id_field: str,
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        iid = candidate_id(item, id_field=id_field, index=index)
        answer = answers[iid]
        ranked.append(
            {
                **item,
                "id": iid,
                "relevance": relevance_of(answer),
                "confidence": answer.get("confidence"),
            }
        )
    ranked.sort(key=lambda row: row["relevance"], reverse=True)
    return ranked


def rank_chunks(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    return list(windows(items, size=RANK_CHUNK))

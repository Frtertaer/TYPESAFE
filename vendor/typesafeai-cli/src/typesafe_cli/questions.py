from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class QuestionError(ValueError):
    pass


ALLOWED_TYPES = {"noul", "choice", "score"}


def load_state(raw: str | dict | list) -> str | dict | list:
    return raw


def parse_state_text(text: str) -> str | dict | list:
    stripped = text.strip()
    if not stripped:
        raise QuestionError("state is empty")
    if stripped[0] in "{[":
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return text
    return text


def load_state_file(path: Path) -> str | dict | list:
    return parse_state_text(path.read_text(encoding="utf-8"))


def load_questions_file(path: Path) -> tuple[dict[str, dict[str, Any]], str | dict | list | None]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise QuestionError("questions file must be a JSON object")
    embedded_state = payload.get("state")
    return load_questions(payload), embedded_state


def load_questions(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if "questions" in data:
        raw = data["questions"]
        if isinstance(raw, list):
            mapping: dict[str, Any] = {}
            for item in raw:
                if not isinstance(item, dict) or "id" not in item:
                    raise QuestionError("list questions must each have an id")
                qid = item["id"]
                if qid in mapping:
                    raise QuestionError(f"duplicate question id: {qid}")
                mapping[qid] = {k: v for k, v in item.items() if k != "id"}
            raw = mapping
        if not isinstance(raw, dict):
            raise QuestionError("questions must be an object or a list")
        source = raw
    else:
        source = {k: v for k, v in data.items() if k != "state"}

    if not source:
        raise QuestionError("no questions provided")

    out: dict[str, dict[str, Any]] = {}
    for qid, question in source.items():
        if not isinstance(qid, str) or not qid:
            raise QuestionError("question ids must be non-empty strings")
        if not isinstance(question, dict):
            raise QuestionError(f"question {qid} must be an object")
        out[qid] = _validate_question(qid, question)
    return out


def _validate_question(qid: str, question: dict[str, Any]) -> dict[str, Any]:
    qtype = question.get("type")
    if qtype not in ALLOWED_TYPES:
        raise QuestionError(f"{qid}: type must be noul, choice, or score")
    instructions = question.get("instructions")
    if instructions is None or instructions == "":
        raise QuestionError(f"{qid}: instructions are required")

    criteria = question.get("criteria")
    if qtype == "choice":
        if not isinstance(criteria, dict) or len(criteria) < 2:
            raise QuestionError(f"{qid}: choice criteria must be a map with at least two options")
    elif qtype == "score":
        if not isinstance(criteria, list) or len(criteria) < 2:
            raise QuestionError(f"{qid}: score criteria must be a list with at least two levels")
    elif criteria is not None and not isinstance(criteria, dict):
        raise QuestionError(f"{qid}: noul criteria must be an object if present")

    validated: dict[str, Any] = {"type": qtype, "instructions": instructions}
    if criteria is not None:
        validated["criteria"] = criteria
    return validated

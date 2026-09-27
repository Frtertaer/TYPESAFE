from __future__ import annotations

import re
from typing import Any

from typesafe_cli.questions import QuestionError
from typesafe_cli.recipes.find import UNTRUSTED

PATTERN_NAMES = ("email", "phone", "money")

_PATTERNS = {
    "email": re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    "phone": re.compile(r"\+?\d[\d.\-\s()]{7,}\d"),
    "money": re.compile(r"\$\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\b\d+\.\d{2}\b"),
}


def find_spans(text: str, pattern: str) -> list[str]:
    if pattern not in _PATTERNS:
        raise QuestionError(f"unknown pattern {pattern!r}; use {', '.join(PATTERN_NAMES)} or --candidates")
    seen: list[str] = []
    for match in _PATTERNS[pattern].findall(text):
        span = match.strip()
        if span and span not in seen:
            seen.append(span)
    return seen


def extract_questions(spans: list[str], question: str) -> dict[str, Any]:
    criteria: dict[str, str | None] = {span: None for span in spans}
    criteria["none"] = "None of the candidate spans is the requested value"
    if len(criteria) < 2:
        raise QuestionError("extract needs at least one candidate span plus none")
    return {
        "value": {
            "type": "choice",
            "instructions": (
                UNTRUSTED
                + f"{question} Pick the verbatim candidate span. "
                "If none of the candidates is correct, choose none."
            ),
            "criteria": criteria,
        }
    }

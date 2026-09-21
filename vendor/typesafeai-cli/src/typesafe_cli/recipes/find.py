from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from typesafe_cli.recipes.thresholds import FIND_CHUNK, FIND_EXISTS_ANSWERED, FIND_EXISTS_PARTIAL


def tag_lines(text: str) -> list[dict[str, str]]:
    raw_lines = text.splitlines()
    width = max(3, len(str(max(len(raw_lines) - 1, 0))))
    return [{"id": f"L{index:0{width}d}", "text": line} for index, line in enumerate(raw_lines)]


def tagged_state(lines: Sequence[dict[str, str]]) -> str:
    return "\n".join(f"{line['id']}| {line['text']}" for line in lines)


def windows(items: Sequence[Any], *, size: int) -> Iterator[list[Any]]:
    if size < 1:
        raise ValueError("window size must be >= 1")
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def find_questions(lines: Sequence[dict[str, str]], query: str) -> dict[str, Any]:
    criteria: dict[str, str | None] = {line["id"]: None for line in lines}
    if len(criteria) < 2:
        criteria["none"] = "No line in this window answers the query"
    return {
        "line": {
            "type": "choice",
            "instructions": (
                "Which tagged line best answers the query? "
                f"Query: {query}. Prefer the most specific matching line id."
            ),
            "criteria": criteria,
        },
        "exists": {
            "type": "noul",
            "instructions": f"Does this document contain an answer to the query `{query}`?",
        },
    }


def verdict_for_exists(exists: float) -> str:
    if exists >= FIND_EXISTS_ANSWERED:
        return "answered"
    if exists >= FIND_EXISTS_PARTIAL:
        return "partial"
    return "absent"


def merge_find_results(
    *,
    lines_by_id: dict[str, str],
    chunks: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    combined: dict[str, float] = {}
    exists_values: list[float] = []
    model = None
    usage = {"input_tokens": 0, "output_tokens": 0}
    for chunk in chunks:
        model = chunk.get("model") or model
        chunk_usage = chunk.get("usage") or {}
        usage["input_tokens"] += int(chunk_usage.get("input_tokens") or 0)
        usage["output_tokens"] += int(chunk_usage.get("output_tokens") or 0)
        answers = chunk["answers"]
        exists_values.append(float(answers["exists"]["noul"]))
        probabilities = answers["line"].get("probabilities") or {}
        for line_id, probability in probabilities.items():
            if line_id == "none":
                continue
            combined[str(line_id)] = max(combined.get(str(line_id), 0.0), float(probability))
    ranked = sorted(combined.items(), key=lambda item: item[1], reverse=True)
    exists = max(exists_values) if exists_values else 0.0
    return {
        "model": model,
        "query_lines": [
            {"id": line_id, "text": lines_by_id.get(line_id, ""), "probability": probability}
            for line_id, probability in ranked
            if line_id in lines_by_id
        ],
        "exists": exists,
        "verdict": verdict_for_exists(exists),
        "usage": usage,
    }


FIND_CHUNK = FIND_CHUNK

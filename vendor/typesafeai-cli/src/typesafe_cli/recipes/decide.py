from __future__ import annotations

from typing import Any

from typesafe_cli.questions import QuestionError
from typesafe_cli.recipes.thresholds import CHOICE_MIN_TOP_P, NOUL_UNCERTAIN_HIGH, NOUL_UNCERTAIN_LOW


def parse_noul_band(raw: str) -> tuple[float, float]:
    try:
        low_s, high_s = raw.split(":", 1)
        low, high = float(low_s), float(high_s)
    except ValueError as exc:
        raise QuestionError("noul band must be low:high, e.g. 0.30:0.70") from exc
    if not 0.0 <= low <= high <= 1.0:
        raise QuestionError("noul band must satisfy 0 <= low <= high <= 1")
    return low, high


def extract_answers(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise QuestionError("answers file must be a JSON object")
    if "answers" in payload and isinstance(payload["answers"], dict):
        return payload["answers"]
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("answers"), dict):
        return data["answers"]
    if payload and all(isinstance(v, dict) and "type" in v for v in payload.values()):
        return payload
    raise QuestionError("could not find answers in file (expected data.answers or answers)")


def apply_decide(
    answers: dict[str, Any],
    *,
    noul_low: float = NOUL_UNCERTAIN_LOW,
    noul_high: float = NOUL_UNCERTAIN_HIGH,
    choice_min_p: float = CHOICE_MIN_TOP_P,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for qid, answer in answers.items():
        if not isinstance(answer, dict):
            raise QuestionError(f"{qid}: answer must be an object")
        qtype = answer.get("type")
        if qtype == "noul":
            probability = float(answer["noul"])
            if probability < noul_low:
                decision = "no"
            elif probability > noul_high:
                decision = "yes"
            else:
                decision = "uncertain"
            out[qid] = {**answer, "decision": decision, "noul": probability}
        elif qtype == "choice":
            probabilities = {str(k): float(v) for k, v in dict(answer.get("probabilities") or {}).items()}
            top_p = max(probabilities.values()) if probabilities else 0.0
            winner = answer.get("choice")
            decision = winner if top_p >= choice_min_p else "uncertain"
            out[qid] = {**answer, "decision": decision, "top_probability": top_p}
        elif qtype == "score":
            out[qid] = {**answer, "decision": answer.get("score")}
        else:
            raise QuestionError(f"{qid}: unsupported answer type {qtype!r}")
    return out

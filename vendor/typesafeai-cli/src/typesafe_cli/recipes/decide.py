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
            out[qid] = {**answer, "decision": decision, "noul": probability, "ignored": False}
        elif qtype == "choice":
            probabilities = {str(k): float(v) for k, v in dict(answer.get("probabilities") or {}).items()}
            top_p = max(probabilities.values()) if probabilities else 0.0
            winner = answer.get("choice")
            decision = winner if top_p >= choice_min_p else "uncertain"
            out[qid] = {**answer, "decision": decision, "top_probability": top_p, "ignored": False}
        elif qtype == "score":
            out[qid] = {**answer, "decision": answer.get("score"), "ignored": False}
        else:
            raise QuestionError(f"{qid}: unsupported answer type {qtype!r}")
    return _ignore_unused_targets(out)


def _ignore_unused_targets(decisions: dict[str, Any]) -> dict[str, Any]:
    targets = [qid for qid in decisions if qid.endswith("_target")]
    if not targets:
        return decisions
    route_id = "operation" if "operation" in decisions else None
    if route_id is None:
        for qid, row in decisions.items():
            if qid.endswith("_target"):
                continue
            if row.get("type") == "choice" and f"{str(row.get('choice') or '').lower()}_target" in decisions:
                route_id = qid
                break
    if route_id is None:
        return decisions
    route = decisions[route_id]
    winner = route.get("choice") if route.get("decision") not in {None, "uncertain"} else route.get("decision")
    if route.get("decision") == "uncertain":
        active_target = None
    else:
        active_target = f"{str(winner).lower()}_target"
    for qid in targets:
        if qid != active_target:
            decisions[qid] = {**decisions[qid], "decision": "ignored", "ignored": True}
    return decisions


def decide_action(decisions: dict[str, Any]) -> dict[str, Any]:
    active = [qid for qid, row in decisions.items() if not row.get("ignored")]
    ignored = [qid for qid, row in decisions.items() if row.get("ignored")]
    blocking = []
    for qid in active:
        row = decisions[qid]
        if row.get("type") == "score":
            continue
        if row.get("decision") == "uncertain":
            blocking.append(qid)
    action = "abstain" if blocking else "act"
    return {
        "active": active,
        "ignored": ignored,
        "action": action,
        "needs_verify": action == "act",
    }

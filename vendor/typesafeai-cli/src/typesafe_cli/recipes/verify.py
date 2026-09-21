from __future__ import annotations

from typing import Any

from typesafe_cli.recipes.thresholds import VERIFY_AUTO_CONFIDENCE


def quote_in_source(source: str, quote: str) -> bool:
    return bool(quote) and quote in source


def verify_questions(*, claim: str) -> dict[str, Any]:
    return {
        "support": {
            "type": "choice",
            "instructions": (
                "Does `source` support `claim`? "
                "supports = the source affirms the claim; "
                "contradicts = the source denies it; "
                "says_nothing = the source is silent."
            ),
            "criteria": {
                "supports": "The source affirms the claim",
                "contradicts": "The source conflicts with the claim",
                "says_nothing": "The source does not address the claim",
            },
        }
    }


def verify_state(*, claim: str, source: str, quote: str | None) -> dict[str, Any]:
    state: dict[str, Any] = {"claim": claim, "source": source}
    if quote:
        state["quote"] = quote
    return state


def interpret_verify(answer: dict[str, Any], *, auto_confidence: float = VERIFY_AUTO_CONFIDENCE) -> dict[str, Any]:
    verdict = answer["choice"]
    confidence = float(answer.get("confidence") or 0.0)
    auto = confidence >= auto_confidence and verdict != "says_nothing"
    return {
        "verdict": verdict,
        "confidence": confidence,
        "auto": auto,
        "review": not auto,
        "probabilities": answer.get("probabilities") or {},
    }

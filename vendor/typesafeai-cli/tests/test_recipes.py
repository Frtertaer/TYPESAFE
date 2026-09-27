from __future__ import annotations

import json
from pathlib import Path

import pytest

from typesafe_cli.questions import QuestionError
from typesafe_cli.recipes.decide import apply_decide, decide_action, parse_noul_band
from typesafe_cli.recipes.extract import PATTERN_NAMES, find_spans
from typesafe_cli.recipes.find import FIND_CHUNK, merge_find_results, tag_lines, verdict_for_exists, windows
from typesafe_cli.recipes.screen import suggested_action
from typesafe_cli.recipes.skills import parse_skill_md
from typesafe_cli.recipes.verify import quote_in_source


def test_noul_band_maps_uncertain_middle():
    answers = {
        "low": {"type": "noul", "noul": 0.1},
        "mid": {"type": "noul", "noul": 0.49},
        "high": {"type": "noul", "noul": 0.91},
        "edge_low": {"type": "noul", "noul": 0.30},
        "edge_high": {"type": "noul", "noul": 0.70},
    }
    decided = apply_decide(answers, noul_low=0.30, noul_high=0.70, choice_min_p=0.60)
    assert decided["low"]["decision"] == "no"
    assert decided["mid"]["decision"] == "uncertain"
    assert decided["high"]["decision"] == "yes"
    assert decided["edge_low"]["decision"] == "uncertain"
    assert decided["edge_high"]["decision"] == "uncertain"
    assert decided["mid"]["noul"] == 0.49


def test_decide_ignores_unused_target_heads():
    answers = {
        "operation": {
            "type": "choice",
            "choice": "TYPE_TEXT",
            "probabilities": {"TYPE_TEXT": 0.9, "CLICK": 0.1},
            "confidence": 0.85,
        },
        "type_text_target": {
            "type": "choice",
            "choice": "1",
            "probabilities": {"1": 0.95, "none": 0.05},
            "confidence": 0.9,
        },
        "click_target": {
            "type": "choice",
            "choice": "invented",
            "probabilities": {"2": 0.99, "none": 0.01},
            "confidence": 0.99,
        },
    }
    decided = apply_decide(answers, noul_low=0.30, noul_high=0.70, choice_min_p=0.60)
    assert decided["operation"]["decision"] == "TYPE_TEXT"
    assert decided["type_text_target"]["decision"] == "1"
    assert decided["click_target"]["ignored"] is True
    assert decided["click_target"]["decision"] == "ignored"


def test_decide_joint_gate_abstains_if_target_is_uncertain():
    answers = {
        "operation": {
            "type": "choice",
            "choice": "CLICK",
            "probabilities": {"CLICK": 0.9, "TYPE_TEXT": 0.1},
            "confidence": 0.8,
        },
        "click_target": {
            "type": "choice",
            "choice": "1",
            "probabilities": {"1": 0.51, "2": 0.49},
            "confidence": 0.05,
        },
    }
    decided = apply_decide(answers, noul_low=0.30, noul_high=0.70, choice_min_p=0.60)
    assert decided["operation"]["decision"] == "CLICK"
    assert decided["click_target"]["decision"] == "uncertain"
    assert decide_action(decided)["action"] == "abstain"
    assert decide_action(decided)["needs_verify"] is False


def test_choice_abstains_when_top_probability_is_low():
    answers = {
        "sure": {
            "type": "choice",
            "choice": "billing",
            "probabilities": {"billing": 0.8, "other": 0.2},
            "confidence": 0.7,
        },
        "split": {
            "type": "choice",
            "choice": "billing",
            "probabilities": {"billing": 0.51, "other": 0.49},
            "confidence": 0.05,
        },
    }
    decided = apply_decide(answers, noul_low=0.30, noul_high=0.70, choice_min_p=0.60)
    assert decided["sure"]["decision"] == "billing"
    assert decided["split"]["decision"] == "uncertain"
    assert decided["split"]["choice"] == "billing"


def test_parse_noul_band():
    assert parse_noul_band("0.30:0.70") == (0.30, 0.70)
    with pytest.raises(QuestionError):
        parse_noul_band("0.8:0.2")


def test_find_tags_and_chunks_lines():
    text = "alpha\nbeta\ngamma"
    lines = tag_lines(text)
    assert [line["id"] for line in lines] == ["L000", "L001", "L002"]
    assert lines[1]["text"] == "beta"
    chunks = list(windows(list(range(10)), size=4))
    assert chunks == [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9]]
    assert FIND_CHUNK <= 255


def test_find_exists_verdict_bands():
    assert verdict_for_exists(0.86) == "answered"
    assert verdict_for_exists(0.50) == "partial"
    assert verdict_for_exists(0.10) == "absent"


def test_find_absent_does_not_treat_ranking_as_evidence():
    lines_by_id = {"L000": "alpha", "L001": "beta"}
    chunks = [
        {
            "model": "jev-1.13.0",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "answers": {
                "exists": {"type": "noul", "noul": 0.08},
                "line": {
                    "type": "choice",
                    "choice": "L000",
                    "probabilities": {"L000": 0.91, "L001": 0.09},
                    "confidence": 0.8,
                },
            },
        }
    ]
    merged = merge_find_results(lines_by_id=lines_by_id, chunks=chunks)
    assert merged["verdict"] == "absent"
    assert merged["query_lines"] == []
    assert merged["usable"] is False


def test_find_does_not_let_one_maybe_window_answer_the_document():
    lines_by_id = {"L000": "a", "L001": "b"}
    chunks = [
        {
            "model": "jev-1.13.0",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "answers": {
                "exists": {"type": "noul", "noul": 0.72},
                "line": {
                    "type": "choice",
                    "choice": "L000",
                    "probabilities": {"L000": 0.8, "none": 0.2},
                    "confidence": 0.7,
                },
            },
        },
        {
            "model": "jev-1.13.0",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "answers": {
                "exists": {"type": "noul", "noul": 0.12},
                "line": {
                    "type": "choice",
                    "choice": "L001",
                    "probabilities": {"L001": 0.9, "none": 0.1},
                    "confidence": 0.6,
                },
            },
        },
    ]
    merged = merge_find_results(lines_by_id=lines_by_id, chunks=chunks)
    assert merged["verdict"] == "answered"
    assert [row["id"] for row in merged["query_lines"]] == ["L000"]
    assert merged["exists"] == 0.72


def test_extract_email_and_money_spans():
    text = "Send the $1,200.00 receipt to billing@example.com and copy ops@example.com."
    emails = find_spans(text, "email")
    money = find_spans(text, "money")
    assert emails == ["billing@example.com", "ops@example.com"]
    assert money == ["$1,200.00"]
    assert set(PATTERN_NAMES) >= {"email", "phone", "money"}


def test_verify_quote_must_appear_in_source():
    source = "Section 4. Uploaded code remains the user's property."
    assert quote_in_source(source, "Uploaded code remains the user's property.")
    assert not quote_in_source(source, "We own all uploaded code.")


def test_screen_action_from_thresholds():
    assert suggested_action(jailbreak=0.9, injection=0.1, sensitive=0.1, harm_score=0.2) == "block"
    assert suggested_action(jailbreak=0.1, injection=0.8, sensitive=0.1, harm_score=0.2) == "block"
    assert suggested_action(jailbreak=0.1, injection=0.1, sensitive=0.85, harm_score=0.2) == "review"
    assert suggested_action(jailbreak=0.1, injection=0.1, sensitive=0.1, harm_score=1.8) == "review"
    assert suggested_action(jailbreak=0.1, injection=0.1, sensitive=0.1, harm_score=0.4) == "pass"


def test_parse_skill_frontmatter(tmp_path: Path):
    path = tmp_path / "typesafe-cli" / "SKILL.md"
    path.parent.mkdir()
    path.write_text(
        "---\nname: typesafe-cli\ndescription: Drive Jev through the CLI.\n---\n\n# typesafe CLI\n\nBody here.\n",
        encoding="utf-8",
    )
    skill = parse_skill_md(path)
    assert skill["name"] == "typesafe-cli"
    assert "Drive Jev" in skill["description"]
    assert "Body here" in skill["excerpt"]

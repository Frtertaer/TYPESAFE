from __future__ import annotations

import pytest

from typesafe_cli.questions import QuestionError, load_questions, parse_state_text


def test_noul_ok():
    questions = load_questions(
        {"urgency": {"type": "noul", "instructions": "Does this convey urgency?"}}
    )
    assert questions["urgency"]["type"] == "noul"


def test_nested_questions_object():
    questions = load_questions(
        {
            "questions": {
                "urgency": {"type": "noul", "instructions": "urgent?"},
            }
        }
    )
    assert "urgency" in questions


def test_questions_list_with_ids():
    questions = load_questions(
        {
            "questions": [
                {"id": "a", "type": "noul", "instructions": "A?"},
                {"id": "b", "type": "noul", "instructions": "B?"},
            ]
        }
    )
    assert set(questions) == {"a", "b"}


def test_choice_one_option_fails():
    with pytest.raises(QuestionError):
        load_questions(
            {
                "dept": {
                    "type": "choice",
                    "instructions": "Which team?",
                    "criteria": {"billing": None},
                }
            }
        )


def test_score_one_level_fails():
    with pytest.raises(QuestionError):
        load_questions(
            {
                "sev": {
                    "type": "score",
                    "instructions": "How bad?",
                    "criteria": ["low"],
                }
            }
        )


def test_missing_instructions_fails():
    with pytest.raises(QuestionError):
        load_questions({"x": {"type": "noul"}})


def test_duplicate_ids_fail():
    with pytest.raises(QuestionError, match="duplicate"):
        load_questions(
            {
                "questions": [
                    {"id": "x", "type": "noul", "instructions": "one"},
                    {"id": "x", "type": "noul", "instructions": "two"},
                ]
            }
        )


def test_parse_json_state():
    assert parse_state_text('{"ticket": "hi"}') == {"ticket": "hi"}
    assert parse_state_text("plain text") == "plain text"

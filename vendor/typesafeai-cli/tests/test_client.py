from __future__ import annotations

from types import SimpleNamespace

import pytest

from typesafe_cli.answers import AnswerError
from typesafe_cli.client import answers_to_dict, system_one, to_sdk_questions
from typesafe_cli.config import Config
from typesafe_sdk import Choice, Noul, Score


def test_noul_answer_dict():
    response = SimpleNamespace(
        model="jev-1.13.0",
        answers={"urgency": SimpleNamespace(noul=0.92)},
        usage=SimpleNamespace(input_tokens=10, output_tokens=2),
    )
    data = answers_to_dict(response)
    assert data["answers"]["urgency"]["type"] == "noul"
    assert data["answers"]["urgency"]["noul"] == 0.92
    assert data["usage"]["input_tokens"] == 10


def test_choice_and_score_keys_are_strings():
    response = SimpleNamespace(
        model="jev-1.13.0",
        answers={
            "dept": SimpleNamespace(
                choice="billing",
                probabilities={"billing": 0.8, "tech": 0.2},
                confidence=0.7,
            ),
            "frustration": SimpleNamespace(
                score=1.2,
                legend={0: "calm", 1: "mad"},
                probabilities={0: 0.2, 1: 0.8},
                confidence=0.5,
            ),
        },
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    data = answers_to_dict(response)
    assert data["answers"]["dept"]["choice"] == "billing"
    assert data["answers"]["frustration"]["legend"] == {"0": "calm", "1": "mad"}
    assert data["answers"]["frustration"]["probabilities"] == {"0": 0.2, "1": 0.8}


def test_to_sdk_questions():
    sdk = to_sdk_questions(
        {
            "urgency": {"type": "noul", "instructions": "urgent?"},
            "dept": {
                "type": "choice",
                "instructions": "team?",
                "criteria": {"a": None, "b": None},
            },
            "sev": {"type": "score", "instructions": "how bad?", "criteria": ["low", "high"]},
        }
    )
    assert isinstance(sdk["urgency"], Noul)
    assert isinstance(sdk["dept"], Choice)
    assert isinstance(sdk["sev"], Score)


def test_system_one_rejects_invented_choice(monkeypatch):
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def system_one(self, **_kwargs):
            return SimpleNamespace(
                model="jev-1.13.0",
                answers={
                    "op": SimpleNamespace(
                        choice="invented",
                        probabilities={"a": 1.0, "b": 0.0},
                        confidence=1.0,
                    )
                },
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    monkeypatch.setattr("typesafe_cli.client.TypeSafeClient", lambda **_k: FakeClient())
    with pytest.raises(AnswerError, match="Invalid TypeSafe"):
        system_one(
            config=Config(api_key="k", base_url="https://api.typesafe.ai", model="jev-latest"),
            state="x",
            questions={"op": {"type": "choice", "instructions": "pick", "criteria": {"a": None, "b": None}}},
        )

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from typesafe_cli.cli import app

runner = CliRunner()


def test_help_exits_zero():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "ask" in result.stdout


def test_auth_status_without_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    result = runner.invoke(app, ["auth", "status"])
    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert body["status"] == "success"
    assert body["data"]["has_key"] is False
    assert "apikey" not in result.stdout.lower()


def test_auth_status_with_key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret-key-value")
    result = runner.invoke(app, ["auth", "status"])
    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert body["data"]["has_key"] is True
    assert "secret-key-value" not in result.stdout


def test_ask_prints_envelope(tmp_path: Path, monkeypatch):
    questions = tmp_path / "q.json"
    questions.write_text('{"urgency": {"type": "noul", "instructions": "urgent?"}}')
    state = tmp_path / "s.json"
    state.write_text('"help now"')
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    monkeypatch.setattr(
        "typesafe_cli.commands.ask.system_one",
        lambda **kwargs: {
            "model": "jev-1.13.0",
            "answers": {"urgency": {"type": "noul", "noul": 0.9}},
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    result = runner.invoke(app, ["ask", "--state-file", str(state), "--questions-file", str(questions)])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["status"] == "success"
    assert body["data"]["answers"]["urgency"]["noul"] == 0.9
    assert body["metadata"]["command"] == "ask"


def test_ask_invalid_questions_exit_2(tmp_path: Path, monkeypatch):
    questions = tmp_path / "q.json"
    questions.write_text('{"x": {"type": "noul"}}')
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    result = runner.invoke(app, ["ask", "--state", "hi", "--questions-file", str(questions)])
    assert result.exit_code == 2
    err = json.loads(result.stderr)
    assert err["status"] == "error"
    assert err["error"]["code"] == "usage"


def test_ask_missing_key_exit_1(tmp_path: Path, monkeypatch):
    questions = tmp_path / "q.json"
    questions.write_text('{"x": {"type": "noul", "instructions": "yes?"}}')
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    result = runner.invoke(app, ["ask", "--state", "hi", "--questions-file", str(questions)])
    assert result.exit_code == 1
    err = json.loads(result.stderr)
    assert err["error"]["code"] == "auth"


def test_noul_command(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    monkeypatch.setattr(
        "typesafe_cli.commands.ask.system_one",
        lambda **kwargs: {
            "model": "jev-1.13.0",
            "answers": {"noul": {"type": "noul", "noul": 0.4}},
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    result = runner.invoke(app, ["noul", "Does this convey urgency?", "--state", "help now"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["metadata"]["command"] == "noul"
    assert body["data"]["answers"]["noul"]["noul"] == 0.4


def test_choice_requires_two_options(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    result = runner.invoke(
        app,
        ["choice", "Which team?", "--option", "billing", "--state", "refund"],
    )
    assert result.exit_code == 2


def test_models_uses_client(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    monkeypatch.setattr(
        "typesafe_cli.commands.models.list_models",
        lambda **kwargs: {
            "models": [{"name": "jev-latest", "description": "flagship", "release_date": "2026-09-15"}]
        },
    )
    result = runner.invoke(app, ["models"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["data"]["models"][0]["name"] == "jev-latest"


def test_smoke_mocked(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    monkeypatch.setattr(
        "typesafe_cli.commands.ask.system_one",
        lambda **kwargs: {
            "model": "jev-1.13.0",
            "answers": {"urgency": {"type": "noul", "noul": 0.99}},
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    result = runner.invoke(app, ["smoke"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["metadata"]["command"] == "smoke"

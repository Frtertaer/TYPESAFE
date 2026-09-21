from __future__ import annotations

import json

from typer.testing import CliRunner

from typesafe_cli.cli import app
from typesafe_cli.schema import command_schema

runner = CliRunner()


def test_schema_lists_phase1_and_phase2_commands():
    schema = command_schema()
    names = {cmd["name"] for cmd in schema["commands"]}
    assert "ask" in names
    assert "noul" in names
    assert "agent" in names
    assert "skills" in names
    assert schema["output"]["stdout"] == "{status, data, metadata}"
    assert schema["workflows"][0]["skill"] == "typesafe-cli"
    assert "cannot access typesafeai-cli environment variables" in schema["secrets"]["rule"]
    assert any("TYPESAFE_*" in item for item in schema["anti_patterns"])


def test_agent_schema_command():
    result = runner.invoke(app, ["agent", "schema"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["status"] == "success"
    assert body["metadata"]["command"] == "agent schema"
    names = {cmd["name"] for cmd in body["data"]["commands"]}
    assert "ask" in names
    assert "skills" in names


def test_compact_schema():
    result = runner.invoke(app, ["agent", "schema", "--compact"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert "anti_patterns" not in body["data"]
    assert "commands" in body["data"]


def test_help_is_text_without_agent_mode():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert result.stdout.lstrip().startswith("{") is False


def test_help_is_json_in_agent_mode():
    result = runner.invoke(app, ["--agent", "--help"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["status"] == "success"
    assert "commands" in body["data"]


def test_ask_help_json_in_agent_mode():
    result = runner.invoke(app, ["--agent", "ask", "--help"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["status"] == "success"
    names = {cmd["name"] for cmd in body["data"]["commands"]}
    assert names == {"ask"} or "ask" in names

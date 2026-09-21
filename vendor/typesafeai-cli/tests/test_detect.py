from __future__ import annotations

from typesafe_cli.detect import detect_agent_info, is_agent_mode, resolve_agent


def test_not_detected_by_default():
    info = detect_agent_info()
    assert info.detected is False
    assert is_agent_mode(argv=["typesafe"]) is False


def test_codex_env(monkeypatch):
    monkeypatch.setenv("CODEX", "1")
    info = detect_agent_info()
    assert info.detected is True
    assert info.name == "codex"
    assert is_agent_mode(argv=["typesafe"]) is True


def test_agent_flag_forces_on():
    assert is_agent_mode(argv=["typesafe", "--agent", "ask"]) is True


def test_no_agent_flag_wins(monkeypatch):
    monkeypatch.setenv("CODEX", "1")
    assert is_agent_mode(argv=["typesafe", "--no-agent"]) is False


def test_force_agent_mode_env(monkeypatch):
    monkeypatch.setenv("FORCE_AGENT_MODE", "true")
    assert is_agent_mode(argv=["typesafe"]) is True


def test_resolve_agent_aliases():
    assert resolve_agent(target="claude") == "claude-code"
    assert resolve_agent(target="grok") == "grok"

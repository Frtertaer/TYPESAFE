from __future__ import annotations

import os
import sys
from contextvars import ContextVar
from dataclasses import dataclass

_force_agent: ContextVar[bool | None] = ContextVar("typesafe_force_agent", default=None)


def set_force_agent(value: bool | None) -> None:
    _force_agent.set(value)

DETECTORS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("claude-code", ("CLAUDECODE", "CLAUDE_CODE")),
    ("cursor", ("CURSOR_AGENT",)),
    ("codex", ("CODEX", "OPENAI_CODEX")),
    ("opencode", ("OPENCODE",)),
    ("aider", ("AIDER",)),
    ("cline", ("CLINE",)),
    ("windsurf", ("WINDSURF_AGENT",)),
    ("github-copilot", ("GITHUB_COPILOT",)),
    ("amazon-q", ("AMAZON_Q", "AWS_Q_DEVELOPER")),
    ("gemini-code", ("GEMINI_CODE_ASSIST",)),
    ("sourcegraph-cody", ("SRC_CODY",)),
    ("grok", ("GROK", "GROK_CODE")),
    ("generic-agent", ("AGENT",)),
)

ALL_ENV_VARS: tuple[str, ...] = tuple(var for _, vars_ in DETECTORS for var in vars_) + (
    "FORCE_AGENT_MODE",
)


@dataclass(frozen=True)
class AgentInfo:
    name: str
    detected: bool


def _truthy(key: str) -> bool:
    value = os.environ.get(key)
    if value is None:
        return False
    return value.lower() in {"1", "true", "yes"}


def detect_agent_info() -> AgentInfo:
    for name, env_vars in DETECTORS:
        if any(_truthy(var) for var in env_vars):
            return AgentInfo(name=name, detected=True)
    return AgentInfo(name="", detected=False)


def is_agent_mode(*, argv: list[str] | None = None) -> bool:
    forced = _force_agent.get()
    if forced is not None:
        return forced
    args = sys.argv if argv is None else argv
    if "--no-agent" in args:
        return False
    if "--agent" in args:
        return True
    if _truthy("FORCE_AGENT_MODE"):
        return True
    return detect_agent_info().detected


def resolve_agent(*, target: str | None = None) -> str:
    if target and target not in {"auto", ""}:
        aliases = {
            "claude": "claude-code",
            "claude-code": "claude-code",
            "cursor": "cursor",
            "codex": "codex",
            "opencode": "opencode",
            "grok": "grok",
            "gemini": "gemini-code",
            "windsurf": "windsurf",
        }
        return aliases.get(target, target)
    info = detect_agent_info()
    return info.name or "generic-agent"

"""Idempotent TypeSafe pointer in the user's global agent files (not the repo)."""

from __future__ import annotations

from pathlib import Path

START = "<!-- typesafe-cli:start -->"
END = "<!-- typesafe-cli:end -->"

SNIPPET = """<!-- typesafe-cli:start -->
## TypeSafe CLI

When the user asks to investigate, triage, verify a claim, rank options, extract a span, screen a message, route a task, or make a typed yes/no or score judgment (Jev, noul, choice, TypeSafe), follow the **typesafe-cli** skill and run `typesafe`. Collect only the named state first. Do not curl TypeSafe.
<!-- typesafe-cli:end -->
"""


def global_agent_paths(*, home: Path, agent: str) -> list[Path]:
    """Home-level files each agent loads on every session.

    Same split as `npx skills add -g`: user config, not the git tree.
    """
    claude = home / ".claude" / "CLAUDE.md"
    codex = home / ".codex" / "AGENTS.md"
    grok_rule = home / ".grok" / "rules" / "typesafe-cli.md"
    agents = home / ".agents" / "AGENTS.md"
    cursor = home / ".cursor" / "AGENTS.md"
    gemini = home / ".gemini" / "GEMINI.md"
    by_agent = {
        "claude-code": [claude],
        "codex": [codex],
        "grok": [grok_rule, claude],
        "cursor": [cursor],
        "gemini-code": [gemini],
        "opencode": [agents],
        "generic-agent": [agents],
    }
    if agent in by_agent:
        return by_agent[agent]
    existing = [p for p in (claude, codex, grok_rule, agents, cursor, gemini) if p.is_file()]
    return existing or [grok_rule]


def upsert_global_agent_files(*, home: Path, agent: str) -> list[Path]:
    written: list[Path] = []
    for path in global_agent_paths(home=home, agent=agent):
        path.parent.mkdir(parents=True, exist_ok=True)
        _upsert_snippet(path)
        written.append(path)
    return written


def _upsert_snippet(path: Path) -> None:
    body = path.read_text(encoding="utf-8") if path.is_file() else ""
    block = SNIPPET.strip() + "\n"
    start = body.find(START)
    end = body.find(END)
    if start != -1 and end != -1 and end > start:
        end += len(END)
        while end < len(body) and body[end] == "\n":
            end += 1
        updated = body[:start].rstrip() + "\n\n" + block + body[end:].lstrip("\n")
        if not updated.endswith("\n"):
            updated += "\n"
    elif body.strip():
        updated = body.rstrip() + "\n\n" + block
    else:
        updated = block
    path.write_text(updated, encoding="utf-8")

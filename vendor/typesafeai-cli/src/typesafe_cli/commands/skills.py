from __future__ import annotations

import urllib.error
import urllib.request
from importlib.resources import files
from pathlib import Path

import typer

from typesafe_cli.detect import detect_agent_info, resolve_agent
from typesafe_cli.io import emit_success, fail

OFFICIAL_SKILL_URL = (
    "https://raw.githubusercontent.com/typesafe-ai/skills/main/skills/typesafe-ai/SKILL.md"
)

SKILL_DIR_BY_AGENT = {
    "claude-code": ".claude/skills",
    "cursor": ".cursor/skills",
    "windsurf": ".windsurf/skills",
    "gemini-code": ".gemini/skills",
    "codex": ".agents/skills",
    "opencode": ".agents/skills",
    "grok": ".agents/skills",
    "generic-agent": ".agents/skills",
}

USER_SKILL_DIR_BY_AGENT = {
    "claude-code": Path.home() / ".claude" / "skills",
    "cursor": Path.home() / ".cursor" / "skills",
    "windsurf": Path.home() / ".windsurf" / "skills",
    "gemini-code": Path.home() / ".gemini" / "skills",
    "codex": Path.home() / ".agents" / "skills",
    "opencode": Path.home() / ".agents" / "skills",
    "grok": Path.home() / ".agents" / "skills",
    "generic-agent": Path.home() / ".agents" / "skills",
}

EXISTING_PROJECT_DIRS = (
    ".agents/skills",
    ".claude/skills",
    ".cursor/skills",
    ".windsurf/skills",
    ".gemini/skills",
)


def install(
    target: str = typer.Option("auto", "--target", help="claude, cursor, codex, grok, or auto"),
    directory: Path | None = typer.Option(None, "--dir", help="Explicit skills directory"),
    project: bool = typer.Option(True, "--project/--global", help="Install into the current project (default)"),
    offline: bool = typer.Option(False, "--offline", help="Use the vendored official skill, skip GitHub"),
) -> None:
    try:
        official = load_official_skill(offline=offline)
        cli_note = _vendored("typesafe-ai", "CLI.md")
        cli_skill = _vendored("typesafe-cli", "SKILL.md")
    except OSError as exc:
        fail(code="request", message=f"could not load skills: {exc}", exit_code=1)

    agent = resolve_agent(target=target)
    dest_root = directory if directory is not None else _skills_root(agent=agent, project=project)
    written: list[str] = []
    try:
        pairs = [
            (dest_root / "typesafe-ai" / "SKILL.md", official),
            (dest_root / "typesafe-ai" / "CLI.md", cli_note),
            (dest_root / "typesafe-cli" / "SKILL.md", cli_skill),
        ]
        for path, body in pairs:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            written.append(str(path))
    except OSError as exc:
        fail(code="request", message=str(exc), exit_code=1)

    emit_success(
        data={
            "agent": agent,
            "written": written,
            "source": "offline-vendored" if offline else "github-or-vendored",
            "note": "Installed typesafe-ai (design) and typesafe-cli (collect state, then typesafe ask). Agents cannot access TYPESAFE_* env vars.",
        },
        command="skills install",
    )


def list_skills() -> None:
    info = detect_agent_info()
    emit_success(
        data={
            "skills": [
                {
                    "name": "typesafe-cli",
                    "description": "Collect local context, then run the typesafe CLI for a Jev judgment.",
                    "type": "skill",
                },
                {
                    "name": "typesafe-ai",
                    "description": "Official TypeSafe skill: design typed judgments and compose them in code.",
                    "type": "skill",
                },
            ],
            "detected_agent": info.name or None,
        },
        command="skills list",
    )


def load_official_skill(*, offline: bool = False) -> str:
    if not offline:
        try:
            with urllib.request.urlopen(OFFICIAL_SKILL_URL, timeout=10) as response:
                text = response.read().decode("utf-8")
            if text.strip().startswith("---") and "typesafe-ai" in text:
                return text
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
    return _vendored("typesafe-ai", "SKILL.md")


def _vendored(*parts: str) -> str:
    path = files("typesafe_cli.data")
    for part in parts:
        path = path / part
    return path.read_text(encoding="utf-8")


def _skills_root(*, agent: str, project: bool) -> Path:
    if project:
        cwd = Path.cwd()
        for rel in EXISTING_PROJECT_DIRS:
            candidate = cwd / rel
            if candidate.is_dir():
                return candidate
        rel = SKILL_DIR_BY_AGENT.get(agent, ".agents/skills")
        return cwd / rel
    return USER_SKILL_DIR_BY_AGENT.get(agent, Path.home() / ".agents" / "skills")

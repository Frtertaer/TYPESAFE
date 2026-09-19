#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Install jev-consult into Hermes, Claude Code, Codex, and Grok only."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ALLOWED = ("hermes", "claude-code", "codex", "grok")
BLOCKED = {
    "cursor",
    "gemini",
    "antigravity",
    "windsurf",
    "cline",
    "aider",
    "all",
    "copilot",
    "opencode",
}
MARKER_START = "<!-- jev-consult:start -->"
MARKER_END = "<!-- jev-consult:end -->"
SNIPPET = """<!-- jev-consult:start -->
## Jev consult (TypeSafe)

On implementation, refactor, architecture, library choice, deletions, or "is this good enough": load the `jev-consult` skill and run its `scripts/jev.py` before acting. The user may give only a plan. Do not ask Jev for facts a tool can check. Never print `TYPESAFE_API_KEY`.
<!-- jev-consult:end -->
"""


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def skill_source() -> Path:
    return repo_root() / "skills" / "jev-consult"


def user_home() -> Path:
    return Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())


def hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env)
    home = user_home()
    for candidate in (home / ".hermes", Path("D:/Hermes/home")):
        if (candidate / "skills").is_dir():
            return candidate
    return home / ".hermes"


def env_file_has_key(path: Path) -> bool:
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() == "TYPESAFE_API_KEY" and value.strip().strip("\"'"):
            return True
    return False


def key_is_set() -> bool:
    if os.environ.get("TYPESAFE_API_KEY", "").strip():
        return True
    root = repo_root()
    hermes = hermes_home()
    for path in (root / ".env", hermes / ".env", user_home() / ".env"):
        if env_file_has_key(path):
            return True
    return False


def report_key() -> None:
    if key_is_set():
        sys.stdout.write("TYPESAFE_API_KEY: set\n")
    else:
        sys.stdout.write(
            "TYPESAFE_API_KEY: missing (copy .env.example to .env or export it; never commit the key)\n"
        )


REPO_FILES = ("AGENTS.md", "CLAUDE.md", ".hermes.md")
REPO_SNIPPET = """<!-- jev-consult:start -->
# jev-consult

User gives a plan. You inspect this repo. At architecture, deleting code, refactor vs rewrite, library choice, or "is this good enough", load `skills/jev-consult/SKILL.md` and run:

```text
python skills/jev-consult/scripts/jev.py ask request.json
```

Thresholds live only in `skills/jev-consult/policy.json`. Do not ask Jev for facts a tool can check. Never print `TYPESAFE_API_KEY`.

After clone, one command wires user-scope Hermes / Claude Code / Codex / Grok Build:

```text
python scripts/install.py
```
<!-- jev-consult:end -->
"""


def targets(home: Path | None = None, hermes: Path | None = None) -> dict[str, dict[str, list[Path]]]:
    home = home or user_home()
    hermes = hermes or hermes_home()
    return {
        "hermes": {
            "skills": [hermes / "skills"],
            "instructions": [],
        },
        "claude-code": {
            "skills": [home / ".claude" / "skills"],
            "instructions": [home / ".claude" / "CLAUDE.md"],
        },
        "codex": {
            "skills": [home / ".codex" / "skills", home / ".agents" / "skills"],
            "instructions": [home / ".codex" / "AGENTS.md"],
        },
        "grok": {
            "skills": [home / ".grok" / "skills"],
            "instructions": [home / ".grok" / "AGENTS.md"],
        },
    }


def parse_agents(raw: str | None) -> list[str]:
    if not raw:
        return list(ALLOWED)
    names = [part.strip().lower() for part in raw.split(",") if part.strip()]
    if not names:
        raise SystemExit("no agents given")
    for name in names:
        if name in BLOCKED or name not in ALLOWED:
            raise SystemExit(
                "refusing agent %r; allowed: %s"
                % (name, ", ".join(ALLOWED))
            )
    return names


def copy_skill(src: Path, dest_parent: Path, dry_run: bool) -> Path:
    dest = dest_parent / "jev-consult"
    if dry_run:
        return dest
    dest_parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    (dest / ".jev-consult-source").write_text(str(src.resolve()) + "\n", encoding="utf-8")
    return dest


def upsert_snippet(path: Path, dry_run: bool, snippet: str | None = None) -> str:
    if dry_run:
        return "upsert " + str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    block = (snippet or SNIPPET).strip() + "\n"
    if MARKER_START in text and MARKER_END in text:
        pre = text.split(MARKER_START)[0]
        post = text.split(MARKER_END, 1)[1]
        text = pre.rstrip() + "\n\n" + block + post.lstrip("\n")
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text = text + ("\n" if text else "") + block
        if not text.endswith("\n"):
            text += "\n"
    path.write_text(text, encoding="utf-8")
    return "wrote " + str(path)


def strip_snippet(path: Path, dry_run: bool) -> str:
    if not path.exists():
        return "missing " + str(path)
    if dry_run:
        return "strip " + str(path)
    text = path.read_text(encoding="utf-8")
    if MARKER_START not in text or MARKER_END not in text:
        return "no marker " + str(path)
    pre = text.split(MARKER_START)[0]
    post = text.split(MARKER_END, 1)[1]
    text = (pre.rstrip() + "\n" + post.lstrip("\n")).strip() + "\n"
    path.write_text(text, encoding="utf-8")
    return "stripped " + str(path)


def install(agents: list[str], dry_run: bool) -> int:
    src = skill_source()
    if not (src / "SKILL.md").is_file():
        raise SystemExit("missing skill at %s" % src)
    mapping = targets()
    for name in agents:
        spec = mapping[name]
        for parent in spec["skills"]:
            dest = copy_skill(src, parent, dry_run)
            sys.stdout.write("%s skill -> %s\n" % (name, dest))
        for instruction in spec["instructions"]:
            sys.stdout.write("%s %s\n" % (name, upsert_snippet(instruction, dry_run)))
    write_repo_instructions(dry_run)
    if not dry_run:
        report_key()
    return 0


def write_repo_instructions(dry_run: bool) -> None:
    root = repo_root()
    for name in REPO_FILES:
        path = root / name
        sys.stdout.write("repo %s\n" % upsert_snippet(path, dry_run, REPO_SNIPPET))


def uninstall(agents: list[str], dry_run: bool) -> int:
    mapping = targets()
    for name in agents:
        spec = mapping[name]
        for parent in spec["skills"]:
            dest = parent / "jev-consult"
            sys.stdout.write("%s remove %s\n" % (name, dest))
            if not dry_run and dest.exists():
                shutil.rmtree(dest)
        for instruction in spec["instructions"]:
            sys.stdout.write("%s %s\n" % (name, strip_snippet(instruction, dry_run)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Copy jev-consult into Hermes, Claude Code, Codex, and Grok only."
    )
    parser.add_argument(
        "--agents",
        help="Comma list. Default: hermes,claude-code,codex,grok. Others are refused.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument(
        "--check-key",
        action="store_true",
        help="Print whether TYPESAFE_API_KEY is set (never the value).",
    )
    args = parser.parse_args(argv)
    if args.check_key:
        report_key()
        return 0 if key_is_set() else 1
    agents = parse_agents(args.agents)
    if args.uninstall:
        return uninstall(agents, args.dry_run)
    return install(agents, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

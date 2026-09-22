#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Install jev-consult into Hermes, Claude Code, Codex, and Grok only."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

HOOK_MARK = "compact_hook.py"
TOOLS_HOOK_MARK = "inventory_hook.py"
PLUGIN_NAME = "jev-compact"

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

You inspect; Jev decides. On every coding or planning task, load `jev-consult` and run its `scripts/jev.py` before you pick approach, keep vs change, architecture, library, delete, or good-enough. If `ask` stdout has no `decision.action`, do not pick architecture, library, or approach. Write the request with `scripts/jev.py scaffold` then `ask`. If you drift, get stuck, loop, or cannot solve, call Jev with that state and a Choice of next moves. If you do not know or do not remember, load `.jev-trace.json` and ask. A hook IDF-shortlists already-installed skills/plugins/MCP then one Jev pick (fail-open) for the user prompt (Claude UserPromptSubmit, Hermes pre_llm_call, Codex UserPromptSubmit in `~/.codex/hooks.json` — trust it in `/hooks` or Codex skips it). If `.jev-tools-miss.json` exists, run `scripts/peer_fill.py --from-miss`; if it prints `no_peer`, run `scripts/catalog_fill.py --from-miss` (Jev pick, inspect, `hermes skills install --yes` one skill); if that prints `no_catalog`, run `scripts/apply_fill.py --from-miss` (one Hermes plugin `--no-enable` or one official MCP). If `.jev-tools.json` exists and `scripts/inventory.py --check-sidecar` prints `fresh`, load those names. Else run `scripts/inventory.py` then Jev Choice on the shortlist. Do not ask the user to pick tools. Hook never auto-installs. Never `--force`. Never npx. Never `claude plugin install`. Never print `TYPESAFE_API_KEY`.
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

You inspect this repo and write code. Jev decides. Load `skills/jev-consult/SKILL.md` and run `ask` before you pick approach, keep vs change, architecture, library, delete, refactor vs rewrite, naming, or good-enough. If `ask` stdout has no `decision.action`, do not pick architecture, library, or approach. Write the request with `jev.py scaffold` then `ask`. If you drift, get stuck, loop, or cannot solve, call Jev with that state and a Choice of next moves. If you do not know or do not remember, load `.jev-trace.json` and ask. At session start, a hook IDF-shortlists already-installed skills/plugins/MCP then one Jev pick (fail-open, never installs). Codex: `install.py` writes `~/.codex/hooks.json` UserPromptSubmit; trust it in `/hooks` or the hook is skipped. If `.jev-tools-miss.json` exists, run `peer_fill.py --from-miss`; if `no_peer`, run `catalog_fill.py --from-miss`; if `no_catalog`, run `apply_fill.py --from-miss`. If `.jev-tools.json` exists and `inventory.py --check-sidecar` prints `fresh`, load those names. Stale or missing: run `inventory.py` then Jev Choice on the shortlist. Skip only tool-checkable facts and mechanical follow-through of a Jev pick already made this session.

```text
python skills/jev-consult/scripts/jev.py ask request.json --trace
python skills/jev-consult/scripts/jev.py scaffold keep_vs_change --out request.json --plan "<task>"
python skills/jev-consult/scripts/inventory.py --task "<task>" --harness auto --write-ask tools.request.json
python skills/jev-consult/scripts/peer_fill.py --from-miss
python skills/jev-consult/scripts/catalog_fill.py --from-miss
python skills/jev-consult/scripts/apply_fill.py --from-miss
python skills/jev-consult/scripts/compare.py --live
python skills/jev-consult/scripts/compact.py transcript.json --history --trace
```

Thresholds live only in `skills/jev-consult/policy.json`. Hook never auto-installs. Never `--force`. Never npx. Never `claude plugin install`. Never print `TYPESAFE_API_KEY`.

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
        head = pre.rstrip() + "\n\n" if pre.strip() else ""
        text = head + block + post.lstrip("\n")
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


def hook_script(skill_dest: Path, mark: str | None = None) -> Path:
    return skill_dest / "scripts" / (mark or HOOK_MARK)


def grok_hook_command(script: Path) -> str:
    exe = sys.executable.replace("\\", "/")
    path = str(script).replace("\\", "/")
    return '"%s" "%s"' % (exe, path)


def upsert_codex_event(
    path: Path, event: str, script: Path, marker: str, timeout: int, dry_run: bool
) -> str:
    if dry_run:
        return "upsert hook %s %s" % (event, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return "invalid json " + str(path)
        if not isinstance(data, dict):
            return "invalid json " + str(path)
    else:
        data = {}
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return "no hooks " + str(path)
    entries = hooks.setdefault(event, [])
    if not isinstance(entries, list):
        entries = []
        hooks[event] = entries
    entries[:] = [entry for entry in entries if marker not in json.dumps(entry)]
    cmd = grok_hook_command(script)
    entries.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": cmd,
                    "commandWindows": cmd,
                    "timeout": timeout,
                    "statusMessage": "Jev tools shortlist",
                }
            ]
        }
    )
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return "wrote hook %s %s" % (event, path)


def strip_codex_event(path: Path, event: str, marker: str, dry_run: bool) -> str:
    if not path.exists():
        return "missing " + str(path)
    if dry_run:
        return "strip hook %s %s" % (event, path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "invalid json " + str(path)
    if not isinstance(data, dict):
        return "invalid json " + str(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return "no hooks " + str(path)
    entries = hooks.get(event)
    if not isinstance(entries, list):
        return "no %s %s" % (event, path)
    kept = [entry for entry in entries if marker not in json.dumps(entry)]
    if len(kept) == len(entries):
        return "no marker " + str(path)
    if kept:
        hooks[event] = kept
    else:
        hooks.pop(event, None)
    if hooks:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return "stripped hook %s %s" % (event, path)
    path.unlink()
    return "removed hook file " + str(path)


def upsert_claude_event(
    settings: Path, event: str, script: Path, marker: str, timeout: int, dry_run: bool
) -> str:
    if dry_run:
        return "upsert hook %s %s" % (event, settings)
    settings.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    if not isinstance(data, dict):
        data = {}
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks
    entries = hooks.setdefault(event, [])
    if not isinstance(entries, list):
        entries = []
        hooks[event] = entries
    entries[:] = [entry for entry in entries if marker not in json.dumps(entry)]
    entries.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": sys.executable,
                    "args": [str(script)],
                    "timeout": timeout,
                }
            ]
        }
    )
    settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return "wrote hook %s %s" % (event, settings)


def upsert_claude_hook(settings: Path, script: Path, dry_run: bool) -> str:
    return upsert_claude_event(settings, "PostToolUse", script, HOOK_MARK, 8, dry_run)


def strip_claude_event(settings: Path, event: str, marker: str, dry_run: bool) -> str:
    if not settings.exists():
        return "missing " + str(settings)
    if dry_run:
        return "strip hook %s %s" % (event, settings)
    data = json.loads(settings.read_text(encoding="utf-8"))
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return "no hooks " + str(settings)
    entries = hooks.get(event)
    if not isinstance(entries, list):
        return "no %s %s" % (event, settings)
    kept = [entry for entry in entries if marker not in json.dumps(entry)]
    if len(kept) == len(entries):
        return "no marker " + str(settings)
    hooks[event] = kept
    settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return "stripped hook %s %s" % (event, settings)


def strip_claude_hook(settings: Path, dry_run: bool) -> str:
    return strip_claude_event(settings, "PostToolUse", HOOK_MARK, dry_run)


def write_grok_event(path: Path, event: str, script: Path, timeout: int, dry_run: bool) -> str:
    if dry_run:
        return "upsert hook " + str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "hooks": {
            event: [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": grok_hook_command(script),
                            "timeout": timeout,
                        }
                    ]
                }
            ]
        }
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return "wrote hook " + str(path)


def write_grok_hook(path: Path, script: Path, dry_run: bool) -> str:
    return write_grok_event(path, "PostToolUse", script, 8, dry_run)


def copy_hermes_plugin(src: Path, dest: Path, dry_run: bool) -> str:
    if dry_run:
        return "plugin -> " + str(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    return "plugin -> " + str(dest)


def enable_hermes_plugin(config: Path, name: str, dry_run: bool) -> str:
    if not config.is_file():
        return "missing " + str(config)
    text = config.read_text(encoding="utf-8")
    if re.search(r"(?m)^\s*-\s+%s\s*$" % re.escape(name), text):
        return "already enabled " + name
    needle = "plugins:\n  enabled:\n"
    if needle not in text:
        return "no plugins.enabled " + str(config)
    if dry_run:
        return "enable " + name
    config.write_text(text.replace(needle, needle + "    - %s\n" % name, 1), encoding="utf-8")
    return "enabled " + name


def disable_hermes_plugin(config: Path, name: str, dry_run: bool) -> str:
    if not config.is_file():
        return "missing " + str(config)
    if dry_run:
        return "disable " + name
    pattern = re.compile(r"(?m)^\s*-\s+%s\s*\n" % re.escape(name))
    text = config.read_text(encoding="utf-8")
    new = pattern.sub("", text, count=1)
    if new == text:
        return "not enabled " + name
    config.write_text(new, encoding="utf-8")
    return "disabled " + name


def install_live_hooks(agents: list[str], dry_run: bool) -> None:
    home = user_home()
    hermes = hermes_home()
    src = skill_source()
    if "claude-code" in agents:
        skill = home / ".claude" / "skills" / "jev-consult"
        settings = home / ".claude" / "settings.json"
        sys.stdout.write(
            "claude-code %s\n" % upsert_claude_hook(settings, hook_script(skill), dry_run)
        )
        sys.stdout.write(
            "claude-code %s\n"
            % upsert_claude_event(
                settings,
                "UserPromptSubmit",
                hook_script(skill, TOOLS_HOOK_MARK),
                TOOLS_HOOK_MARK,
                20,
                dry_run,
            )
        )
    if "grok" in agents:
        skill = home / ".grok" / "skills" / "jev-consult"
        sys.stdout.write(
            "grok %s\n"
            % write_grok_hook(home / ".grok" / "hooks" / "jev-compact.json", hook_script(skill), dry_run)
        )
        sys.stdout.write(
            "grok %s\n"
            % write_grok_event(
                home / ".grok" / "hooks" / "jev-tools.json",
                "UserPromptSubmit",
                hook_script(skill, TOOLS_HOOK_MARK),
                20,
                dry_run,
            )
        )
    if "hermes" in agents:
        dest = hermes / "plugins" / PLUGIN_NAME
        sys.stdout.write("hermes %s\n" % copy_hermes_plugin(src / "hermes-plugin", dest, dry_run))
        sys.stdout.write("hermes %s\n" % enable_hermes_plugin(hermes / "config.yaml", PLUGIN_NAME, dry_run))
    if "codex" in agents:
        skill = home / ".codex" / "skills" / "jev-consult"
        hooks = home / ".codex" / "hooks.json"
        sys.stdout.write("codex live mutate: none (no PostToolUse)\n")
        sys.stdout.write(
            "codex %s\n"
            % upsert_codex_event(
                hooks,
                "UserPromptSubmit",
                hook_script(skill, TOOLS_HOOK_MARK),
                TOOLS_HOOK_MARK,
                20,
                dry_run,
            )
        )


def uninstall_live_hooks(agents: list[str], dry_run: bool) -> None:
    home = user_home()
    hermes = hermes_home()
    if "claude-code" in agents:
        settings = home / ".claude" / "settings.json"
        sys.stdout.write("claude-code %s\n" % strip_claude_hook(settings, dry_run))
        sys.stdout.write(
            "claude-code %s\n" % strip_claude_event(settings, "UserPromptSubmit", TOOLS_HOOK_MARK, dry_run)
        )
    if "grok" in agents:
        for name in ("jev-compact.json", "jev-tools.json"):
            path = home / ".grok" / "hooks" / name
            sys.stdout.write("grok remove %s\n" % path)
            if not dry_run and path.exists():
                path.unlink()
    if "hermes" in agents:
        dest = hermes / "plugins" / PLUGIN_NAME
        sys.stdout.write("hermes remove %s\n" % dest)
        if not dry_run and dest.exists():
            shutil.rmtree(dest)
        sys.stdout.write("hermes %s\n" % disable_hermes_plugin(hermes / "config.yaml", PLUGIN_NAME, dry_run))
    if "codex" in agents:
        sys.stdout.write(
            "codex %s\n"
            % strip_codex_event(home / ".codex" / "hooks.json", "UserPromptSubmit", TOOLS_HOOK_MARK, dry_run)
        )


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
    install_live_hooks(agents, dry_run)
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
    uninstall_live_hooks(agents, dry_run)
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

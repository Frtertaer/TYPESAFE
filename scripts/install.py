#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Install jev-consult into Hermes, Claude Code, Codex, and Grok only."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import stat
import subprocess
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


_SOURCE: Path | None = None


def set_source(raw: str | None) -> Path | None:
    """Point the installer at an unpacked release bundle (or a bare
    jev-consult skill dir) instead of the repo layout around __file__."""
    global _SOURCE
    if raw is None:
        _SOURCE = None
        return None
    cand = Path(raw).expanduser()
    if not cand.is_dir():
        raise SystemExit("--source %s is not a directory" % raw)
    _SOURCE = cand.resolve()
    return _SOURCE


def repo_root() -> Path:
    if _SOURCE is not None:
        return _SOURCE
    return Path(__file__).resolve().parent.parent


def skill_source() -> Path:
    root = repo_root()
    if _SOURCE is not None and (root / "SKILL.md").is_file():
        # --source pointed straight at a jev-consult skill directory
        return root
    return root / "skills" / "jev-consult"


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
        if stripped.lower().startswith("export "):
            stripped = stripped[7:].strip()
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


def _rmtree_fix(func, path: str, _exc) -> None:
    """rmtree onexc/onerror handler: clear the read-only bit and retry.

    Windows refuses to unlink read-only files (and transient AV/indexer
    locks surface as PermissionError), which otherwise aborts
    reinstall/uninstall."""
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass
    func(path)


def _rmtree(path: Path) -> None:
    # onexc replaces onerror in 3.12; same (func, path, exc) call shape.
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_rmtree_fix)
    else:
        shutil.rmtree(path, onerror=_rmtree_fix)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        os.chmod(path, stat.S_IWRITE)
        path.unlink()


def _remove_path(dest: Path) -> None:
    """Remove a file/symlink/dir at dest; rmtree refuses symlinks."""
    if dest.is_symlink() or not dest.is_dir():
        _unlink(dest)
    else:
        _rmtree(dest)


def copy_skill(src: Path, dest_parent: Path, dry_run: bool) -> Path:
    dest = dest_parent / "jev-consult"
    if dry_run:
        return dest
    dest_parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        _remove_path(dest)
    shutil.copytree(src, dest)
    (dest / ".jev-consult-source").write_text(str(src.resolve()) + "\n", encoding="utf-8")
    return dest


def _remove_blocks(text: str) -> tuple[str, int]:
    """Remove every complete jev-consult marker block.

    Returns (cleaned text, index of the first removed block or -1).
    """
    first = -1
    pos = text.find(MARKER_START)
    while pos >= 0:
        end = text.find(MARKER_END, pos)
        if end < 0:
            break
        if first < 0:
            first = pos
        text = text[:pos] + text[end + len(MARKER_END):]
        pos = text.find(MARKER_START, pos)
    return text, first


def upsert_snippet(path: Path, dry_run: bool, snippet: str | None = None) -> str:
    if dry_run:
        return "upsert " + str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    block = (snippet or SNIPPET).strip() + "\n"
    cleaned, first = _remove_blocks(text)
    if first >= 0:
        head = cleaned[:first].rstrip()
        tail = cleaned[first:].lstrip("\r\n")
        text = (head + "\n\n" if head else "") + block + tail
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
    cleaned, first = _remove_blocks(text)
    if first < 0:
        return "no marker " + str(path)
    text = (cleaned[:first].rstrip() + "\n" + cleaned[first:].lstrip("\n")).strip() + "\n"
    path.write_text(text, encoding="utf-8")
    return "stripped " + str(path)


def hook_script(skill_dest: Path, mark: str | None = None) -> Path:
    return skill_dest / "scripts" / (mark or HOOK_MARK)


def _hook_interpreter() -> str:
    """Interpreter path recorded in hook commands. Under the PyInstaller
    exe this is the staged runtime copy (JEV_HOOK_PYTHON), not the
    downloaded exe the user may delete after install."""
    return os.environ.get("JEV_HOOK_PYTHON", "").strip() or sys.executable


def grok_hook_command(script: Path) -> str:
    exe = _hook_interpreter().replace("\\", "/")
    path = str(script).replace("\\", "/")
    return '"%s" "%s"' % (exe, path)


def _entry_is_ours(entry, marker: str) -> bool:
    blob = json.dumps(entry)
    return marker in blob and "jev-consult" in blob


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
    entries[:] = [entry for entry in entries if not _entry_is_ours(entry, marker)]
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
    kept = [entry for entry in entries if not _entry_is_ours(entry, marker)]
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
    if settings.exists():
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return "invalid json " + str(settings)
        if not isinstance(data, dict):
            return "invalid json " + str(settings)
    else:
        data = {}
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks
    entries = hooks.setdefault(event, [])
    if not isinstance(entries, list):
        entries = []
        hooks[event] = entries
    entries[:] = [entry for entry in entries if not _entry_is_ours(entry, marker)]
    entries.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": _hook_interpreter(),
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
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "invalid json " + str(settings)
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return "no hooks " + str(settings)
    entries = hooks.get(event)
    if not isinstance(entries, list):
        return "no %s %s" % (event, settings)
    kept = [entry for entry in entries if not _entry_is_ours(entry, marker)]
    if len(kept) == len(entries):
        return "no marker " + str(settings)
    if kept:
        hooks[event] = kept
    else:
        hooks.pop(event, None)
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
    if dest.exists() or dest.is_symlink():
        _remove_path(dest)
    shutil.copytree(src, dest)
    return "plugin -> " + str(dest)


_ENABLED_RE = re.compile(r"(?m)^plugins:\n  enabled:\n")


def _enabled_span(text: str) -> tuple[int, int] | None:
    """(start, end) offsets of the plugins.enabled list items, or None."""
    match = _ENABLED_RE.search(text)
    if match is None:
        return None
    start = pos = match.end()
    for line in text[start:].splitlines(keepends=True):
        stripped = line.strip()
        if stripped and len(line) - len(line.lstrip(" ")) <= 2:
            break
        pos += len(line)
    return (start, pos)


def enable_hermes_plugin(config: Path, name: str, dry_run: bool) -> str:
    if not config.is_file():
        return "missing " + str(config)
    text = config.read_text(encoding="utf-8")
    span = _enabled_span(text)
    if span is None:
        return "no plugins.enabled " + str(config)
    block = text[span[0]:span[1]]
    if re.search(r"(?m)^\s*-\s+%s\s*$" % re.escape(name), block):
        return "already enabled " + name
    if dry_run:
        return "enable " + name
    config.write_text(text[:span[0]] + "    - %s\n" % name + text[span[0]:], encoding="utf-8")
    return "enabled " + name


def disable_hermes_plugin(config: Path, name: str, dry_run: bool) -> str:
    if not config.is_file():
        return "missing " + str(config)
    if dry_run:
        return "disable " + name
    text = config.read_text(encoding="utf-8")
    span = _enabled_span(text)
    if span is None:
        return "no plugins.enabled " + str(config)
    block = text[span[0]:span[1]]
    pattern = re.compile(r"(?m)^\s*-\s+%s\s*\n" % re.escape(name))
    new_block = pattern.sub("", block, count=1)
    if new_block == block:
        return "not enabled " + name
    config.write_text(text[:span[0]] + new_block + text[span[1]:], encoding="utf-8")
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
        if not dry_run and (dest.exists() or dest.is_symlink()):
            _remove_path(dest)
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
            if not dry_run and (dest.exists() or dest.is_symlink()):
                _remove_path(dest)
        for instruction in spec["instructions"]:
            sys.stdout.write("%s %s\n" % (name, strip_snippet(instruction, dry_run)))
    uninstall_live_hooks(agents, dry_run)
    return 0


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def env_report(agents: list[str]) -> dict:
    """Resolved install layout: agent list, homes, target paths, which exist."""
    home = user_home()
    hermes = hermes_home()
    tmap = targets(home, hermes)
    rendered = {
        name: {kind: [str(p) for p in paths] for kind, paths in groups.items()}
        for name, groups in tmap.items()
    }
    existing = sorted(
        str(p)
        for groups in tmap.values()
        for paths in groups.values()
        for p in paths
        if p.exists()
    )
    return {
        "agents": list(agents),
        "home": str(home),
        "hermes_home": str(hermes),
        "source": str(repo_root()),
        "targets": rendered,
        "existing": existing,
        "policy": os.environ.get("JEV_POLICY", "").strip() or "default",
        "key_set": key_is_set(),
    }


def detected_agents(report: dict) -> list[str]:
    """Harnesses that already have at least one install target on disk."""
    existing = set(report.get("existing") or [])
    tmap = report.get("targets") or {}
    found = []
    for name in ALLOWED:
        groups = tmap.get(name) or {}
        paths = [p for paths in groups.values() for p in paths]
        if any(p in existing for p in paths):
            found.append(name)
    return found


def harness_env_paths(agents: list[str]) -> list[Path]:
    """Dotenv files the setup key prompt writes to: each selected harness's
    .env, the repo (or bundle) .env, and ~/.env. The last two are locations
    jev.py/doctor.py actually read, so the key works outside the repo too."""
    home = user_home()
    hermes = hermes_home()
    mapping = {
        "hermes": hermes / ".env",
        "claude-code": home / ".claude" / ".env",
        "codex": home / ".codex" / ".env",
        "grok": home / ".grok" / ".env",
    }
    paths = []
    for name in agents:
        path = mapping.get(name)
        if path is not None and path not in paths:
            paths.append(path)
    for extra in (repo_root() / ".env", home / ".env"):
        if extra not in paths:
            paths.append(extra)
    return paths


def write_api_key(path: Path, value: str) -> str:
    """Upsert TYPESAFE_API_KEY in a dotenv file. Never prints the value."""
    if "\n" in value or "\r" in value or "\x00" in value:
        raise SystemExit("refusing to write a TYPESAFE_API_KEY containing a newline")
    out = []
    wrote = False
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                candidate = stripped
                if candidate.lower().startswith("export "):
                    candidate = candidate[7:].strip()
                key = candidate.split("=", 1)[0].strip()
                if key == "TYPESAFE_API_KEY":
                    if not wrote:
                        out.append("TYPESAFE_API_KEY=" + value)
                        wrote = True
                    continue
            out.append(line)
    if not wrote:
        out.append("TYPESAFE_API_KEY=" + value)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, "\n".join(out).rstrip("\n") + "\n")
    _lock_down_env(path)
    return "wrote " + str(path)


def _lock_down_env(path: Path) -> None:
    """Restrict a written .env to the owner: chmod 600 on POSIX, an
    owner-only ACL on Windows where mode bits are a no-op. Never fatal -
    a warning to stderr is all a failure earns."""
    if os.name == "nt":
        user = os.environ.get("USERNAME") or getpass.getuser()
        try:
            proc = subprocess.run(
                [
                    "icacls",
                    str(path),
                    "/inheritance:r",
                    "/grant:r",
                    "%s:F" % user,
                ],
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            sys.stderr.write(
                "install.py: warning: could not set ACL on %s: %s\n" % (path, exc)
            )
            return
        if proc.returncode != 0:
            sys.stderr.write(
                "install.py: warning: icacls %s exited %s\n" % (path, proc.returncode)
            )
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _doctor_summary(data: object, rc: int) -> str:
    """'PASS (2 harnesses ok, 2 not installed)'-style verdict line.

    Absent harnesses (doctor reports them as skipped presence rows) are
    not failures; only a present harness's failing checks are.
    """
    verdict = "PASS" if rc == 0 else "FAIL"
    if not isinstance(data, dict):
        return verdict
    checks = data.get("checks")
    if not isinstance(checks, list):
        checks = []
    checks = [c for c in checks if isinstance(c, dict)]
    absent_raw = data.get("absent")
    absent = sorted(
        a for a in (absent_raw if isinstance(absent_raw, list) else []) if a
    )
    failing_agents = sorted(
        {
            c.get("agent")
            for c in checks
            if c.get("agent") not in (None, "*")
            and not c.get("ok")
            and not c.get("suppressed")
        }
    )
    other_failed = sorted(
        {
            c.get("check")
            for c in checks
            if c.get("agent") == "*"
            and not c.get("ok")
            and not c.get("suppressed")
        }
    )
    ok_agents = sorted(
        {
            c.get("agent")
            for c in checks
            if c.get("agent") not in (None, "*") and not c.get("skipped")
        }
        - set(failing_agents)
    )
    parts = []
    if failing_agents:
        parts.append("harnesses failing: " + ", ".join(failing_agents))
    if other_failed:
        parts.append("checks failing: " + ", ".join(other_failed))
    if ok_agents:
        parts.append(
            "%d harness%s ok" % (len(ok_agents), "" if len(ok_agents) == 1 else "es")
        )
    if absent:
        parts.append(
            "%d not installed" % len(absent)
        )
    line = "%s (%s)" % (verdict, "; ".join(parts)) if parts else verdict
    live = data.get("live") if isinstance(data, dict) else None
    probes = live.get("probes") if isinstance(live, dict) else None
    if isinstance(probes, dict):
        fallback = live.get("fallback")
        # A limited probe is itself a failing live_probe check — gate
        # 'installed' on the agent's *other* checks, so a broken setup
        # doesn't read as installed just because its CLI answered 429.
        setup_broken = {
            c.get("agent")
            for c in checks
            if c.get("agent") not in (None, "*")
            and c.get("check") != "live_probe"
            and not c.get("ok")
            and not c.get("suppressed")
            and not c.get("skipped")
        }
        for agent in ALLOWED:
            if (probes.get(agent) or {}).get("status") != "limited":
                continue
            if agent in setup_broken:
                line += "\n%s: CLI rate-limited (setup checks failing — see above)" % agent
            elif fallback and fallback != agent:
                line += "\n%s: installed (currently rate-limited — same setup works in %s)" % (
                    agent,
                    fallback,
                )
            else:
                line += "\n%s: installed (currently rate-limited)" % agent
    return line


def run_doctor(agents: list[str], live: bool = False) -> int:
    """Run the bundled doctor scoped to the harnesses the installer just
    wrote (--agents), so a missing harness reads as 'not installed', not
    as a broken install. --live additionally probes each harness's CLI."""
    doctor = skill_source() / "scripts" / "doctor.py"
    if not doctor.is_file():
        sys.stdout.write("doctor: skipped (missing %s)\n" % doctor)
        return 0
    cmd = [sys.executable, str(doctor), "--agents", ",".join(agents)]
    if live:
        cmd += ["--live"]
    if "hermes" in agents:
        cmd += ["--hermes-home", str(hermes_home())]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    try:
        data = json.loads(proc.stdout)
    except (TypeError, ValueError):
        data = None
    sys.stdout.write("doctor: %s\n" % _doctor_summary(data, proc.returncode))
    return proc.returncode


def _console_handle(stream) -> bool:
    """True only for a real Windows console handle.

    isatty() reports character devices like NUL as ttys on Windows, so
    `install.cmd < nul` would otherwise look interactive; GetConsoleMode
    succeeds only on console input/output handles.
    """
    try:
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = ctypes.c_ulong()
        return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    except (ImportError, OSError, ValueError):
        return False


def _tty() -> bool:
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
    except (OSError, ValueError):
        return False
    if os.name == "nt":
        return _console_handle(sys.stdin) and _console_handle(sys.stdout)
    return True


def _prompt(text: str) -> str:
    try:
        return input(text).strip()
    except EOFError:
        return ""


def _setup_menu() -> str:
    sys.stdout.write(
        "\njev-consult setup\n"
        "  1) Install\n"
        "  2) Uninstall\n"
        "  3) Check (doctor)\n"
        "  4) Exit\n"
    )
    return _prompt("choice [1-4]: ")


DEFAULT_KEY_HELP = "ask your Jev/TypeSafe admin (or copy .env.example to .env)"


def key_help() -> str:
    """Where to get TYPESAFE_API_KEY; JEV_KEY_HELP_URL overrides the
    default text with e.g. an org key portal URL."""
    return os.environ.get("JEV_KEY_HELP_URL", "").strip() or DEFAULT_KEY_HELP


def _setup_key(agents: list[str]) -> None:
    sys.stdout.write("get a TYPESAFE_API_KEY: %s\n" % key_help())
    if key_is_set():
        sys.stdout.write("TYPESAFE_API_KEY: already set; Enter keeps it\n")
    try:
        value = getpass.getpass(
            "TYPESAFE_API_KEY (input hidden; Enter skips): "
        ).strip()
    except EOFError:
        return
    if not value:
        sys.stdout.write("TYPESAFE_API_KEY: skipped\n")
        return
    for path in harness_env_paths(agents):
        sys.stdout.write("%s\n" % write_api_key(path, value))


def run_setup(agents_arg: str | None = None, live: bool = False) -> int:
    """Interactive menu driven by detected harnesses; Enter exits.
    live=True adds a doctor --live CLI probe after Install/Check."""
    report = env_report(list(ALLOWED))
    found = detected_agents(report)
    if agents_arg:
        agents = parse_agents(agents_arg)
    else:
        sys.stdout.write(
            "harnesses detected: %s\n" % (", ".join(found) if found else "none")
        )
        raw = _prompt("agents [%s]: " % ",".join(found or list(ALLOWED)))
        agents = parse_agents(raw) if raw else (found or list(ALLOWED))
    while True:
        choice = _setup_menu()
        if choice in ("", "4", "q", "exit"):
            return 0
        if choice == "1":
            _setup_key(agents)
            install(agents, False)
            run_doctor(agents, live)
        elif choice == "2":
            uninstall(agents, False)
        elif choice == "3":
            run_doctor(agents, live)
        else:
            sys.stdout.write("unknown choice %r\n" % choice)


def emit_env(report: dict, jq: str | None, out: str | None) -> int:
    text = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if out:
        try:
            _atomic_write(Path(out), text)
        except OSError as exc:
            sys.stderr.write("install.py env: --out %s failed: %s\n" % (out, exc))
    if jq is None:
        sys.stdout.write(text)
        return 0
    value = report
    parts = jq.split(".")
    i = 0
    while i < len(parts):
        part = parts[i]
        if isinstance(value, dict) and part in value:
            value = value[part]
            i += 1
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
            i += 1
        elif isinstance(value, dict):
            hit = False
            for j in range(len(parts), i + 1, -1):
                literal = ".".join(parts[i:j])
                if literal in value:
                    value = value[literal]
                    i = j
                    hit = True
                    break
            if not hit:
                sys.stderr.write(
                    "install.py env: unknown jq key %r; env has: %s\n"
                    % (jq, ", ".join(sorted(report)))
                )
                return 2
        else:
            sys.stderr.write(
                "install.py env: unknown jq key %r; env has: %s\n"
                % (jq, ", ".join(sorted(report)))
            )
            return 2
    sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")
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
    parser.add_argument(
        "--env",
        action="store_true",
        help="Print the resolved install config JSON (agents, home, "
        "hermes_home, source, targets, existing, policy, key_set) and exit.",
    )
    parser.add_argument(
        "--jq",
        help="With --env: print one report field (dotted dig), rc 2 on unknown key.",
    )
    parser.add_argument(
        "--out", help="With --env: also write the report JSON to PATH (fail-open)."
    )
    parser.add_argument(
        "--source",
        metavar="DIR",
        help="Install from DIR (an unpacked release bundle holding "
        "skills/jev-consult, or a bare jev-consult skill dir) instead of "
        "the repo layout around install.py.",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Interactive menu: Install / Uninstall / Check (doctor) / "
        "Exit. Runs automatically when invoked with no args in a TTY; "
        "ignored when other action flags are given.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="After install/check, run doctor --live: probe each detected "
        "harness's CLI once and report rate-limited harnesses with a "
        "fallback hint.",
    )
    raw = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw)
    if args.source:
        set_source(args.source)
    if args.check_key:
        report_key()
        return 0 if key_is_set() else 1
    if args.env:
        return emit_env(env_report(parse_agents(args.agents)), args.jq, args.out)
    # --live is a modifier, not an action: `--live` alone in a terminal still
    # opens the menu (Install/Uninstall/Check), just with live probing on.
    wants_setup = (args.setup and not (args.uninstall or args.agents or args.dry_run)) or (
        (not raw or set(raw) == {"--live"}) and _tty()
    )
    if wants_setup:
        if not _tty():
            sys.stderr.write(
                "install.py --setup: interactive mode needs a TTY; "
                "pass explicit flags (e.g. --agents) for non-interactive use\n"
            )
            return 2
        return run_setup(args.agents, live=args.live)
    agents = parse_agents(args.agents)
    if args.uninstall:
        return uninstall(agents, args.dry_run)
    rc = install(agents, args.dry_run)
    if args.live and not args.dry_run and rc == 0:
        rc = run_doctor(agents, live=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())

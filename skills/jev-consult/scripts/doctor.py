#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""doctor.py - verify a jev-consult install per harness. Read-only; never installs.

Checks (per harness): skill copied, hooks registered where that harness reads
them, TYPESAFE_API_KEY resolvable (presence only - never printed), policy.json
loads, decisions log reachable. Exit 0 when every check passes, 1 on any
failure, 2 on bad usage. Degrades gracefully: a missing piece is a failed
check, not a crash.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _watch  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
POLICY_PATH = SKILL_DIR / "policy.json"
PLUGIN_NAME = "jev-compact"
COMPACT_MARK = "compact_hook.py"
TOOLS_MARK = "inventory_hook.py"
ALLOWED = ("hermes", "claude-code", "codex", "grok")

HINTS = {
    "skill": "run python scripts/install.py --agents <agent>",
    "plugin_dir": "run python scripts/install.py --agents hermes",
    "plugin_enabled": "add - jev-compact under plugins.enabled in config.yaml",
    "compact_hook": "run python scripts/install.py --agents <agent>",
    "inventory_hook": "run python scripts/install.py --agents <agent>",
    "jev-compact.json": "run python scripts/install.py --agents grok",
    "jev-tools.json": "run python scripts/install.py --agents grok",
    "hooks": "create .claude/settings.json with a hooks block or run python scripts/install.py --agents claude-code",
    "api_key": "set TYPESAFE_API_KEY in the environment or a .env file",
    "policy": "restore skills/jev-consult/policy.json",
    "hooks_json": "fix or delete the malformed hooks file; it blocks hook registration",
}


def _hint(name: str) -> str | None:
    return HINTS.get(name)


def user_home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home())


def hermes_home(home: Path) -> Path:
    for candidate in (home / ".hermes", Path("D:/Hermes/home")):
        if (candidate / "skills").is_dir() or (candidate / "plugins").is_dir():
            return candidate
    return home / ".hermes"


def _check(agent: str, name: str, ok: bool, detail: str) -> dict:
    return {"agent": agent, "check": name, "ok": bool(ok), "detail": detail}


def _has_hook_entry(hooks: object, event: str, marker: str) -> bool:
    if not isinstance(hooks, dict):
        return False
    entries = hooks.get(event)
    if not isinstance(entries, list):
        return False
    return any(marker in json.dumps(entry) for entry in entries)


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _json_validity_check(agent: str, name: str, path: Path) -> dict:
    """ok when the file is absent or parses as JSON; fails on malformed JSON."""
    if not path.is_file():
        return _check(agent, name, True, "absent %s" % path)
    try:
        json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except ValueError as exc:
        return _check(agent, name, False, "invalid JSON in %s: %s" % (path, exc))
    except OSError as exc:
        return _check(agent, name, False, "unreadable %s: %s" % (path, exc))
    return _check(agent, name, True, "valid")


def _skill_check(agent: str, skill_dirs: list[Path]) -> dict:
    for parent in skill_dirs:
        if (parent / "jev-consult" / "SKILL.md").is_file():
            return _check(agent, "skill", True, str(parent / "jev-consult"))
    return _check(agent, "skill", False, "jev-consult/SKILL.md missing under %s" % skill_dirs)


def check_hermes(home: Path, hermes: Path) -> list[dict]:
    out = [_skill_check("hermes", [hermes / "skills"])]
    dest = hermes / "plugins" / PLUGIN_NAME
    out.append(_check("hermes", "plugin_dir", dest.is_dir(), str(dest)))
    config = hermes / "config.yaml"
    enabled = False
    detail = "missing " + str(config)
    if config.is_file():
        try:
            text = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        enabled = ("- %s" % PLUGIN_NAME) in text
        detail = "enabled" if enabled else "not in plugins.enabled"
    out.append(_check("hermes", "plugin_enabled", enabled, detail))
    return out


def check_claude(home: Path) -> list[dict]:
    settings = home / ".claude" / "settings.json"
    out = [
        _skill_check("claude-code", [home / ".claude" / "skills"]),
        _json_validity_check("claude-code", "hooks_json", settings),
    ]
    data = _load_json(settings)
    if data is None:
        out.append(_check("claude-code", "hooks", False, "missing/invalid " + str(settings)))
        return out
    hooks = data.get("hooks") if isinstance(data, dict) else None
    out.append(
        _check(
            "claude-code",
            "compact_hook",
            _has_hook_entry(hooks, "PostToolUse", COMPACT_MARK),
            "PostToolUse",
        )
    )
    out.append(
        _check(
            "claude-code",
            "inventory_hook",
            _has_hook_entry(hooks, "UserPromptSubmit", TOOLS_MARK),
            "UserPromptSubmit",
        )
    )
    return out


def check_grok(home: Path) -> list[dict]:
    out = [_skill_check("grok", [home / ".grok" / "skills"])]
    for name, event, mark in (
        ("jev-compact.json", "PostToolUse", COMPACT_MARK),
        ("jev-tools.json", "UserPromptSubmit", TOOLS_MARK),
    ):
        path = home / ".grok" / "hooks" / name
        out.append(_json_validity_check("grok", "hooks_json", path))
        data = _load_json(path)
        ok = _has_hook_entry((data or {}).get("hooks") if isinstance(data, dict) else None, event, mark)
        out.append(_check("grok", name, ok, event))
    return out


def check_codex(home: Path) -> list[dict]:
    out = [
        _skill_check("codex", [home / ".codex" / "skills", home / ".agents" / "skills"])
    ]
    hooks_path = home / ".codex" / "hooks.json"
    out.append(_json_validity_check("codex", "hooks_json", hooks_path))
    data = _load_json(hooks_path)
    hooks = data.get("hooks") if isinstance(data, dict) else None
    out.append(
        _check(
            "codex",
            "inventory_hook",
            _has_hook_entry(hooks, "UserPromptSubmit", TOOLS_MARK),
            "UserPromptSubmit in %s" % hooks_path,
        )
    )
    return out


def _env_file_has_key(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size > 65536:
            return False
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        if line.split("=", 1)[0].strip() == "TYPESAFE_API_KEY":
            return True
    return False


def check_common(home: Path, hermes: Path) -> list[dict]:
    out = []
    key_set = bool(os.environ.get("TYPESAFE_API_KEY")) or any(
        _env_file_has_key(p)
        for p in (home / ".env", hermes / ".env", Path.cwd() / ".env")
    )
    out.append(_check("*", "api_key", key_set, "set" if key_set else "missing"))
    policy_ok = False
    detail = "missing " + str(POLICY_PATH)
    data = _load_json(POLICY_PATH)
    if isinstance(data, dict):
        policy_ok = bool(data.get("question_soft_max"))
        detail = "ok" if policy_ok else "no question_soft_max"
    out.append(_check("*", "policy", policy_ok, detail))
    log = Path(os.environ.get("JEV_CONSULT_LOG") or (home / ".cache" / "jev-consult" / "decisions.jsonl"))
    lines = 0
    if log.is_file():
        try:
            lines = len(log.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            pass
    out.append(_check("*", "decisions_log", True, "%s (%d lines)" % (log, lines)))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a jev-consult install. Read-only.")
    parser.add_argument("--agents", default=",".join(ALLOWED))
    parser.add_argument("--home", help="Override user home (tests).")
    parser.add_argument("--hermes-home", help="Override Hermes home (tests).")
    parser.add_argument("--quiet", action="store_true", help="Report only failing checks")
    parser.add_argument(
        "--only",
        default="",
        help="Comma-separated check names to run (e.g. skills,hooks_json); default: all.",
    )
    parser.add_argument("--out", metavar="PATH", default="", help="Also write the result JSON to PATH (with --watch: append each tick line)")
    parser.add_argument(
        "--watch",
        type=float,
        metavar="SECONDS",
        help="Re-run the checks every S seconds, emitting a status tick per pass (JEV_DOCTOR_WATCH_MAX caps ticks).",
    )
    parser.add_argument("--max-ticks", metavar="N", type=int, default=0, help="With --watch: stop after N ticks (overrides the JEV_*_WATCH_MAX env)")
    parser.add_argument("--watch-max", metavar="S", type=float, default=0.0, help="With --watch: stop after S elapsed seconds")
    args = parser.parse_args(argv)
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    bad = [a for a in agents if a not in ALLOWED]
    if bad:
        sys.stderr.write("unknown agents: %s\n" % ", ".join(bad))
        return 2
    home = Path(args.home) if args.home else user_home()
    hermes = Path(args.hermes_home) if args.hermes_home else hermes_home(home)
    only = {n.strip() for n in args.only.split(",") if n.strip()}

    def collect() -> list[dict]:
        checks: list[dict] = check_common(home, hermes)
        if "hermes" in agents:
            checks += check_hermes(home, hermes)
        if "claude-code" in agents:
            checks += check_claude(home)
        if "grok" in agents:
            checks += check_grok(home)
        if "codex" in agents:
            checks += check_codex(home)
        if only:
            checks = [c for c in checks if c["check"] in only]
        return checks

    if args.watch:
        import time as _time
        from datetime import datetime, timezone

        max_ticks = _watch.cap("JEV_DOCTOR_WATCH_MAX", args.max_ticks)
        dead = _watch.deadline(getattr(args, "watch_max", 0.0))
        count = 0
        last: dict = {}
        while True:
            cur = collect()
            failed = sum(1 for c in cur if not c["ok"])
            last = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "checks": len(cur),
                "failed": failed,
                "ok": failed == 0,
            }
            _watch.emit(last, args.out, quiet=args.quiet, bad=not last["ok"])
            count += 1
            if max_ticks and count >= max_ticks:
                break
            if dead and _time.time() >= dead:
                break
            _time.sleep(args.watch)
        return 0 if last["ok"] else 1
    checks = collect()
    ok = all(c["ok"] for c in checks)
    for check in checks:
        if not check["ok"]:
            hint = _hint(check["check"])
            if hint:
                check["hint"] = hint.replace("<agent>", check["agent"])
    shown = checks if not args.quiet else [c for c in checks if not c["ok"]]
    text = json.dumps({"ok": ok, "checks": shown}, indent=2) + "\n"
    sys.stdout.write(text)
    if args.out:
        try:
            Path(args.out).write_text(text, encoding="utf-8")
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (args.out, exc))
            return 1
        sys.stderr.write("wrote %s\n" % args.out)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

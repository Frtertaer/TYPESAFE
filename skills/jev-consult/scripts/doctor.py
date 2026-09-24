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
import csv
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _watch  # noqa: E402
from inventory import _policy_float_key  # noqa: E402
from policy_lint import lint_policy  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
POLICY_PATH = SKILL_DIR / "policy.json"
PLUGIN_NAME = "jev-compact"
COMPACT_MARK = "compact_hook.py"
TOOLS_MARK = "inventory_hook.py"
ALLOWED = ("hermes", "claude-code", "codex", "grok")

# Every check name collect() can emit; --schema lists it and tests pin it.
CHECK_NAMES = (
    "skill",
    "plugin_dir",
    "plugin_enabled",
    "hooks_json",
    "hooks",
    "compact_hook",
    "inventory_hook",
    "jev-compact.json",
    "jev-tools.json",
    "api_key",
    "policy",
    "policy_lint",
    "decisions_log",
    "progress_ledger",
)

DOCTOR_SCHEMA_ROWS = {
    "ok": {"required": True, "type": "boolean, true when every check passed"},
    "checks": {"required": True, "type": "list[check]"},
    "check.agent": {"required": True, "type": "string, harness name or *"},
    "check.check": {"required": True, "type": "check name: " + "|".join(CHECK_NAMES)},
    "check.ok": {"required": True, "type": "boolean"},
    "check.detail": {"required": True, "type": "string, human-readable evidence"},
    "check.hint": {"required": False, "type": "string, remediation hint (failing checks only)"},
    "check.suppressed": {"required": False, "type": "boolean, true when --baseline marked this failure known"},
}

BASELINE_FIELDS = ("agent", "check")

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
    "policy_lint": "fix the flagged keys in skills/jev-consult/policy.json (python skills/jev-consult/scripts/policy_lint.py)",
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


def _env_file_max_bytes() -> int:
    """Cap on .env file size scanned for key presence. Threshold lives in
    policy.json (env_file_max_bytes, 65536 default)."""
    return int(_policy_float_key("env_file_max_bytes", 65536.0))


def _env_file_has_key(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size > _env_file_max_bytes():
            return False
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if key == "TYPESAFE_API_KEY":
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
    env_policy = os.environ.get("JEV_POLICY", "").strip()
    policy_path = Path(env_policy) if env_policy else POLICY_PATH
    detail = "missing " + str(policy_path)
    data = _load_json(policy_path)
    if isinstance(data, dict):
        policy_ok = bool(data.get("question_soft_max"))
        detail = "ok" if policy_ok else "no question_soft_max"
    out.append(_check("*", "policy", policy_ok, detail))
    if isinstance(data, dict):
        try:
            findings = lint_policy(data)
        except Exception:
            findings = []
        errors = sum(1 for f in findings if f.get("severity") == "error")
        warns = sum(1 for f in findings if f.get("severity") == "warn")
        first = next(
            (f for f in findings if f.get("severity") == "error"), None
        )
        lint_detail = "errors=%d warnings=%d" % (errors, warns)
        if first is not None:
            lint_detail += " (%s: %s)" % (
                first.get("path") or first.get("rule") or "?",
                first.get("message") or "?",
            )
        out.append(_check("*", "policy_lint", errors == 0, lint_detail))
    raw_log = os.environ.get("JEV_CONSULT_LOG") or ""
    if raw_log.strip() == "0":
        out.append(_check("*", "decisions_log", True, "disabled (JEV_CONSULT_LOG=0)"))
        return out
    log = Path(raw_log) if raw_log else home / ".cache" / "jev-consult" / "decisions.jsonl"
    lines = 0
    if log.is_file():
        try:
            lines = len(log.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            pass
    out.append(_check("*", "decisions_log", True, "%s (%d lines)" % (log, lines)))
    return out


def check_progress(repo: Path) -> list[dict]:
    """Read-only health check on the opt-in progress ledger under the repo.

    Absent ledger is fine (progress is opt-in). A present one must open
    read-only, pass PRAGMA quick_check and agree with its .heads anchor."""
    db_path = repo / ".devin" / "progress.sqlite3"
    if not db_path.is_file():
        return [_check("*", "progress_ledger", True, "absent")]
    try:
        db = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    except (OSError, sqlite3.Error, ValueError) as exc:
        return [_check("*", "progress_ledger", False, "unreadable %s: %s" % (db_path, exc))]
    try:
        try:
            quick = db.execute("PRAGMA quick_check").fetchone()
        except sqlite3.Error as exc:
            return [_check("*", "progress_ledger", False, "not a sqlite db: %s" % exc)]
        if not quick or quick[0] != "ok":
            return [_check("*", "progress_ledger", False, "quick_check: %s" % (quick[0] if quick else "?"))]
        try:
            heads = {row[0]: row[1] for row in db.execute("SELECT stage_id, seal FROM heads")}
        except sqlite3.Error as exc:
            return [_check("*", "progress_ledger", False, "heads table: %s" % exc)]
    finally:
        db.close()
    anchor_path = Path(str(db_path) + ".heads")
    anchor_note = ", no anchor"
    if anchor_path.is_file():
        anchor = _load_json(anchor_path)
        if not isinstance(anchor, dict):
            return [_check("*", "progress_ledger", False, "anchor unreadable: %s" % anchor_path)]
        if anchor != heads:
            return [_check(
                "*", "progress_ledger", False,
                "anchor diverges: %d seals vs %d heads" % (len(anchor), len(heads)),
            )]
        anchor_note = ", anchor ok"
    return [_check(
        "*", "progress_ledger", True,
        "ok (%d stage%s%s)" % (len(heads), "" if len(heads) == 1 else "s", anchor_note),
    )]


def _atomic_write(path, text):
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        _watch.atomic_replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    _watch.fix_stdio()
    if _watch.maybe_version(sys.argv[1:] if argv is None else argv):
        return 0
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
    parser.add_argument("--verdict", metavar="PATH", default="", help="Write a slim verdict JSON ({verdict, ticks, checks, failed, agents}) to PATH when finished (with --watch, refreshed every tick; ticks counts passes). '-' prints it to stdout.")
    parser.add_argument(
        "--watch",
        type=float,
        metavar="SECONDS",
        help="Re-run the checks every S seconds, emitting a status tick per pass (JEV_DOCTOR_WATCH_MAX caps ticks).",
    )
    parser.add_argument("--max-ticks", metavar="N", type=int, default=0, help="With --watch: stop after N ticks (overrides the JEV_*_WATCH_MAX env)")
    parser.add_argument("--watch-max", metavar="S", type=float, default=0.0, help="With --watch: stop after S elapsed seconds")
    parser.add_argument("--unchanged-max", metavar="N", type=int, default=0, help="With --watch: stop after N consecutive identical ticks (volatile ts/elapsed_s ignored)")
    parser.add_argument("--fail-fast", action="store_true", help="With --watch: stop after the first failing tick.")
    parser.add_argument("--jq", metavar="KEY", default="", help="Print just this dotted-path field of the {ok,checks} payload (e.g. ok); unknown key exits 2")
    parser.add_argument("--env", action="store_true", help="Print the resolved env config JSON: {env: {JEV_*/TYPESAFE_* masked dump}, count, watch_max, watch_secs, watch_quiet, policy} (secret-looking names/values masked to <set>)")
    parser.add_argument("--schema", action="store_true", help="Print the {ok,checks} payload key contract and check-name catalog (--json emits the object) and exit")
    parser.add_argument("--json", action="store_true", help="With --schema: emit the contract object instead of text rows (the normal payload is already JSON)")
    parser.add_argument("--report", metavar="PATH", default="", help="Also write a markdown report (verdict line + per-check table with hints) to PATH")
    parser.add_argument("--md", action="store_true", help="Print the same markdown report to stdout instead of the JSON payload")
    parser.add_argument("--matrix", action="store_true", help="Print a check-name x harness markdown grid instead of the JSON payload (cells: yes/NO/suppressed/-; '--' column is the wildcard '*' agent; with --json emits a {check: {agent: status}} object)")
    parser.add_argument("--jsonl", action="store_true", help="Print each check as one JSON line instead of the {ok,checks} payload (for piping)")
    parser.add_argument("--csv", action="store_true", help="Print the checks as CSV rows (check,agent,ok,hint; --keys a,b overrides the columns) instead of the JSON payload")
    parser.add_argument("--keys", metavar="a,b", default="", help="With --jsonl/--csv: keep only these check keys in each row / as the columns (rc 2 on an empty list)")
    parser.add_argument("--baseline", metavar="PATH", default="", help="Mark checks recorded as failing in PATH (written by --baseline-write) as suppressed: they still print but do not fail the run, watch ticks, or verdict; '-' reads the baseline JSON from stdin")
    parser.add_argument("--baseline-write", metavar="PATH", default="", help="Snapshot the currently failing checks to PATH for later --baseline runs")
    parser.add_argument("--self-test", action="store_true", help="Run every check against a synthetic empty HOME; exit 1 when no check fails")
    args = parser.parse_args(argv)
    if args.schema:
        rows = {key: dict(row) for key, row in DOCTOR_SCHEMA_ROWS.items()}
        if args.json:
            sys.stdout.write(json.dumps(rows, indent=2) + "\n")
        else:
            for key in rows:
                sys.stdout.write(
                    "%s: %s (%s)\n"
                    % (key, rows[key]["type"], "required" if rows[key]["required"] else "optional")
                )
        return 0
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    bad = [a for a in agents if a not in ALLOWED]
    if bad:
        sys.stderr.write("unknown agents: %s\n" % ", ".join(bad))
        return 2
    home = Path(args.home) if args.home else user_home()
    hermes = Path(args.hermes_home) if args.hermes_home else hermes_home(home)
    only = {n.strip() for n in args.only.split(",") if n.strip()}
    baseline_keys: set | None = None
    if args.baseline:
        baseline_keys = _watch.load_baseline(args.baseline, BASELINE_FIELDS)

    def _apply_baseline(checks_now: list[dict]) -> int:
        """Mark failing checks whose (agent, check) key is in the baseline."""
        if baseline_keys is None:
            return 0
        n = 0
        for c in checks_now:
            if not c["ok"] and _watch.baseline_key(c, BASELINE_FIELDS) in baseline_keys:
                c["suppressed"] = True
                n += 1
        return n

    if getattr(args, "env", False):
        secretish = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|AUTH|CREDENTIAL)", re.IGNORECASE)
        blob = re.compile(
            r"apikey_[A-Za-z0-9]{20,}_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_-]{20,}"
        )
        env: dict[str, str] = {}
        for name in sorted(os.environ):
            if not (name.startswith("JEV_") or name.startswith("TYPESAFE_")):
                continue
            value = os.environ[name]
            env[name] = (
                "<set>"
                if (value and (secretish.search(name) or blob.search(value)))
                else value
            )
        try:
            watch_secs = float(os.environ.get("JEV_DOCTOR_WATCH_SECS", "") or 0)
        except ValueError:
            watch_secs = 0.0
        payload = {
            "env": env,
            "count": len(env),
            "watch_max": _watch.cap("JEV_DOCTOR_WATCH_MAX", None),
            "watch_secs": watch_secs,
            "watch_quiet": _watch.quiet("JEV_DOCTOR_WATCH_QUIET", False),
            "policy": os.environ.get("JEV_POLICY", "").strip() or "default",
            "env_file_max_bytes": _env_file_max_bytes(),
        }
        if getattr(args, "jq", ""):
            node, found = _watch.dig(payload, args.jq)
            if not found:
                sys.stderr.write(
                    "bad --jq key %r (payload has: %s)\n"
                    % (args.jq, ", ".join(sorted(payload)))
                )
                return 2
            sys.stdout.write(json.dumps(node, ensure_ascii=False) + "\n")
            return 0
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        sys.stdout.write(text)
        if getattr(args, "out", ""):
            try:
                _atomic_write(Path(args.out), text)
            except OSError as exc:
                sys.stderr.write("cannot write %s: %s\n" % (args.out, exc))
        return 0

    def collect() -> list[dict]:
        checks: list[dict] = check_common(home, hermes)
        checks += check_progress(Path.cwd())
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

    def _verdict_payload(checks_now: list[dict], ticks: int = 1) -> dict:
        agents: dict[str, bool] = {}
        for c in checks_now:
            if c.get("suppressed"):
                continue
            agents[c["agent"]] = agents.get(c["agent"], True) and c["ok"]
        return {
            "verdict": "pass"
            if all(c["ok"] or c.get("suppressed") for c in checks_now)
            else "fail",
            "ticks": ticks,
            "checks": len(checks_now),
            "failed": sum(
                1 for c in checks_now if not c["ok"] and not c.get("suppressed")
            ),
            "suppressed": sum(1 for c in checks_now if c.get("suppressed")),
            "agents": agents,
        }

    def _write_verdict(
        checks_now: list[dict], ticks: int = 1, elapsed_s=None
    ) -> bool:
        if not args.verdict:
            return True
        payload = _verdict_payload(checks_now, ticks)
        if elapsed_s is not None:
            payload["elapsed_s"] = elapsed_s
        return _watch.write_verdict(args.verdict, payload)

    if baseline_keys is not None:
        sys.stderr.write(
            "baseline: loaded %d known failure(s)\n" % len(baseline_keys)
        )
    if args.self_test:
        with tempfile.TemporaryDirectory() as tmp:
            thome = Path(tmp)
            thermes = thome / ".hermes"
            checks = (
                check_common(thome, thermes)
                + check_hermes(thome, thermes)
                + check_claude(thome)
                + check_grok(thome)
                + check_codex(thome)
            )
        failed = sum(1 for c in checks if not c["ok"])
        ok = failed > 0
        if args.jq == "":
            if args.quiet and ok:
                return 0
            sys.stdout.write(
                "self-test: %s failed=%d/%d\n"
                % ("ok" if ok else "FAIL", failed, len(checks))
            )
        else:
            payload = {
                "self_test": "ok" if ok else "FAIL",
                "checks": len(checks),
                "failed": failed,
            }
            node, found = _watch.dig(payload, args.jq)
            if not found:
                sys.stderr.write(
                    "bad --jq key %r (payload has: %s)\n"
                    % (args.jq, ", ".join(sorted(payload)))
                )
                return 2
            sys.stdout.write(json.dumps(node, ensure_ascii=False) + "\n")
        return 0 if ok else 1
    if args.baseline_write:
        failing = [
            {"agent": c["agent"], "check": c["check"]}
            for c in collect()
            if not c["ok"]
        ]
        try:
            _atomic_write(
                Path(args.baseline_write),
                json.dumps({"findings": failing}, indent=2) + "\n",
            )
        except OSError as exc:
            sys.stderr.write(
                "cannot write --baseline-write %s: %s\n" % (args.baseline_write, exc)
            )
            return 1
        sys.stderr.write(
            "wrote baseline %s (%d failing checks)\n" % (args.baseline_write, len(failing))
        )
    if args.watch:
        import time as _time
        from datetime import datetime, timezone

        max_ticks = _watch.cap("JEV_DOCTOR_WATCH_MAX", args.max_ticks)
        dead = _watch.deadline("JEV_DOCTOR_WATCH_SECS", getattr(args, "watch_max", 0.0))
        count = 0
        last: dict = {}
        last_checks: list[dict] = []
        verdict_ok = True
        prev_ok: bool | None = None
        prev_tick: dict | None = None
        unchanged = 0
        watch_t0 = _time.time()
        while True:
            cur = collect()
            _apply_baseline(cur)
            failed = sum(1 for c in cur if not c["ok"] and not c.get("suppressed"))
            ok = failed == 0
            last = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "checks": len(cur),
                "failed": failed,
                "suppressed": sum(1 for c in cur if c.get("suppressed")),
                "ok": ok,
                "ok_changed": prev_ok is not None and ok != prev_ok,
                "elapsed_s": round(_time.time() - watch_t0, 2),
            }
            prev_ok = ok
            _watch.emit_or_jq(last, getattr(args, "jq", ""), args.out, quiet=_watch.quiet("JEV_DOCTOR_WATCH_QUIET", args.quiet), bad=not last["ok"])
            last_checks = cur
            count += 1
            sys.stderr.write(
                "watch tick=%d ok=%s failed=%d\n" % (count, last["ok"], failed)
            )
            if verdict_ok and not _write_verdict(
                last_checks, count, elapsed_s=round(_time.time() - watch_t0, 2)
            ):
                verdict_ok = False  # warn once, stop retrying
            if args.fail_fast and not ok:
                break
            if _watch.same_tick(prev_tick, last):
                unchanged += 1
            else:
                unchanged = 0
            prev_tick = dict(last)
            if getattr(args, "unchanged_max", 0) and unchanged >= args.unchanged_max:
                sys.stderr.write("watch: %d consecutive identical ticks\n" % unchanged)
                break
            if max_ticks and count >= max_ticks:
                break
            if dead and _time.time() >= dead:
                break
            _time.sleep(args.watch)
        if args.verdict and verdict_ok and not _write_verdict(
            last_checks, count, elapsed_s=round(_time.time() - watch_t0, 2)
        ):
            return 1
        return 0 if last["ok"] else 1
    checks = collect()
    suppressed = _apply_baseline(checks)
    if suppressed:
        sys.stderr.write(
            "baseline: suppressed %d known failure(s)\n" % suppressed
        )
    ok = all(c["ok"] or c.get("suppressed") for c in checks)
    for check in checks:
        if not check["ok"]:
            hint = _hint(check["check"])
            if hint:
                check["hint"] = hint.replace("<agent>", check["agent"])
    shown = checks if not args.quiet else [c for c in checks if not c["ok"]]
    payload = {"ok": ok, "checks": shown, "suppressed": suppressed}
    text = json.dumps(payload, indent=2) + "\n"
    if args.out:
        try:
            _atomic_write(Path(args.out), text)
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (args.out, exc))
            return 1
        sys.stderr.write("wrote %s\n" % args.out)
    if args.verdict and not _write_verdict(checks):
        return 1
    if getattr(args, "jq", ""):
        node, found = _watch.dig(payload, args.jq)
        if not found:
            sys.stderr.write(
                "bad --jq key %r (payload has: %s)\n"
                % (args.jq, ", ".join(sorted(payload)))
            )
            return 2
        sys.stdout.write(json.dumps(node, ensure_ascii=False) + "\n")
        return 0
    def _report_lines() -> list:
        rep = [
            "# doctor report",
            "",
            "verdict: %s" % ("pass" if ok else "fail"),
            "",
            "| check | agent | ok | hint |",
            "| --- | --- | --- | --- |",
        ]
        for c in shown:
            rep.append(
                "| %s | %s | %s | %s |"
                % (
                    c.get("check") or "",
                    c.get("agent") or "",
                    "yes" if c.get("ok") else ("suppressed" if c.get("suppressed") else "NO"),
                    c.get("hint") or "",
                )
            )
        return rep

    if getattr(args, "report", ""):
        try:
            _atomic_write(Path(args.report), "\n".join(_report_lines()) + "\n")
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (args.report, exc))
            return 1
        sys.stderr.write("wrote %s\n" % args.report)
    if getattr(args, "md", False):
        sys.stdout.write("\n".join(_report_lines()) + "\n")
        return 0 if ok else 1
    if getattr(args, "matrix", False):
        agents = list(ALLOWED) + ["*"]
        grid: dict[str, dict[str, str]] = {}
        for c in shown:
            cell = (
                "suppressed" if c.get("suppressed")
                else "yes" if c.get("ok")
                else "NO"
            )
            grid.setdefault(c["check"], {})[c["agent"]] = cell
        if getattr(args, "json", False):
            matrix = {
                name: {a: grid[name].get(a, "-") for a in agents}
                for name in CHECK_NAMES
                if name in grid
            }
            sys.stdout.write(json.dumps(matrix, indent=2, sort_keys=True) + "\n")
            return 0 if ok else 1
        col = "--"  # '*' renders as an md list marker; use -- for the wildcard column
        lines = [
            "| check | " + " | ".join(agents[:-1] + [col]) + " |",
            "| --- |" + " --- |" * len(agents),
        ]
        for name in CHECK_NAMES:
            if name not in grid:
                continue
            row = grid[name]
            lines.append(
                "| %s | %s |"
                % (name, " | ".join(row.get(a, "-") for a in agents))
            )
        sys.stdout.write("\n".join(lines) + "\n")
        return 0 if ok else 1
    key_sel = getattr(args, "keys", "") or ""
    keys = [k.strip() for k in key_sel.split(",") if k.strip()]
    if (getattr(args, "jsonl", False) or getattr(args, "csv", False)) and key_sel and not keys:
        sys.stderr.write("--keys names no fields\n")
        return 2
    if getattr(args, "csv", False):
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        if keys:
            writer.writerow(keys)
            for c in shown:
                writer.writerow(
                    [
                        json.dumps(c.get(k), sort_keys=True)
                        if isinstance(c.get(k), (dict, list))
                        else str(c.get(k) if c.get(k) is not None else "")
                        for k in keys
                    ]
                )
        else:
            writer.writerow(["check", "agent", "ok", "hint"])
            for c in shown:
                writer.writerow(
                    [
                        c.get("check") or "",
                        c.get("agent") or "",
                        "suppressed" if c.get("suppressed") else ("yes" if c.get("ok") else "no"),
                        c.get("hint") or "",
                    ]
                )
        sys.stdout.write(buf.getvalue())
        return 0 if ok else 1
    if getattr(args, "jsonl", False):
        for c in shown:
            row = (
                {k: c.get(k) for k in keys}
                if keys and isinstance(c, dict)
                else c
            )
            sys.stdout.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        return 0 if ok else 1
    sys.stdout.write(text)
    return 0 if ok else 1


if __name__ == "__main__":
    _watch.exit_safely(main())

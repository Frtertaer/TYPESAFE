#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""smoke.py - offline end-to-end sanity for the jev-consult pack.

Runs each script's offline path in a temp dir: policy loads + policy_lint,
jev scaffold + lint (no network ask), inventory scan, compact --fake,
decisions.py stats over a fixture log, trace.py init + state, doctor.
Prints JSON {ok, steps:[{name, ok, detail}]}; exit 0/1. Never calls
the Jev API. Safe for CI.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _watch  # noqa: E402

SKILL_DIR = SCRIPTS.parent


def _step(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


STEP_TIMEOUT = 60.0


def _run(
    argv: list[str],
    cwd: Path | None = None,
    env: dict | None = None,
    inp: str | None = None,
    timeout: float | None = None,
) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, *argv],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=STEP_TIMEOUT if timeout is None else timeout,
        env=env,
        input=inp,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def step_policy(tmp: Path) -> dict:
    path = SKILL_DIR / "policy.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _step("policy", False, "policy.json unreadable: %s" % exc)
    ok = isinstance(data, dict) and bool(data.get("question_soft_max"))
    return _step("policy", ok, "keys=%d" % len(data) if ok else "missing question_soft_max")


def step_policy_lint(tmp: Path) -> dict:
    rc, out = _run([str(SCRIPTS / "policy_lint.py")])
    return _step("policy_lint", rc == 0, out.strip().splitlines()[-1][:120] if out.strip() else "rc=%d" % rc)


def step_jev_scaffold_lint(tmp: Path) -> dict:
    req = tmp / "smoke.request.json"
    state = tmp / "smoke.state.json"
    state.write_text(json.dumps({"plan": "smoke"}), encoding="utf-8")
    rc, out = _run(
        [
            str(SCRIPTS / "jev.py"),
            "scaffold",
            "approach",
            "--out",
            str(req),
            "--state",
            str(state),
            "--option",
            "approach=a:b",
        ]
    )
    if rc != 0 or not req.is_file():
        return _step("jev_scaffold", False, out.strip()[:160])
    rc, out = _run([str(SCRIPTS / "jev.py"), "lint", str(req)])
    return _step("jev_scaffold_lint", rc == 0, out.strip()[:160] or "clean")


def step_inventory(tmp: Path) -> dict:
    home = tmp / "home"
    hermes = tmp / "hermes"
    (home / ".codex" / "skills" / "s").mkdir(parents=True)
    (home / ".codex" / "skills" / "s" / "SKILL.md").write_text(
        "---\nname: smoke-skill\n---\n", encoding="utf-8"
    )
    rc, out = _run(
        [
            str(SCRIPTS / "inventory.py"),
            "--harness",
            "codex",
            "--home",
            str(home),
            "--hermes-home",
            str(hermes),
            "--task",
            "smoke",
            "--include",
            "smoke-skill",
        ]
    )
    ok = rc == 0 and "smoke-skill" in out
    return _step("inventory", ok, "rc=%d" % rc if ok else out.strip()[:160])


def step_compact_fake(tmp: Path) -> dict:
    transcript = tmp / "t.json"
    transcript.write_text(
        json.dumps(
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "ok"},
            ]
        ),
        encoding="utf-8",
    )
    rc, out = _run(
        [str(SCRIPTS / "compact.py"), str(transcript), "--history", "--fake", "--min-reduction", "0"]
    )
    ok = rc == 0 and '"stats"' in out
    return _step("compact_fake", ok, "rc=%d" % rc if ok else out.strip()[:160])


def step_decisions(tmp: Path) -> dict:
    log = tmp / "decisions.jsonl"
    log.write_text(
        '\n'.join(
            json.dumps({"ts": 1700000000, "jev_status": s, "harness": "smoke"})
            for s in ("winner", "none")
        )
        + "\n",
        encoding="utf-8",
    )
    rc, out = _run([str(SCRIPTS / "decisions.py"), "--file", str(log), "--json"])
    ok = False
    if rc == 0:
        try:
            ok = json.loads(out).get("total") == 2
        except ValueError:
            ok = False
    return _step("decisions", ok, "rc=%d" % rc if ok else out.strip()[:160])


def step_trace(tmp: Path) -> dict:
    trace_file = tmp / ".jev-trace.json"
    rc, out = _run(
        [str(SCRIPTS / "trace.py"), "--file", str(trace_file), "init", "--plan", "smoke"]
    )
    if rc != 0 or not trace_file.is_file():
        return _step("trace", False, out.strip()[:160] or "rc=%d" % rc)
    rc, out = _run(
        [str(SCRIPTS / "trace.py"), "--file", str(trace_file), "state"]
    )
    ok = False
    if rc == 0:
        try:
            ok = isinstance(json.loads(out), dict)
        except ValueError:
            ok = False
    return _step("trace", ok, "rc=%d" % rc if ok else out.strip()[:160])


def step_skill_lint(tmp: Path) -> dict:
    rc, out = _run([str(SCRIPTS / "skill_lint.py"), str(SKILL_DIR / "SKILL.md")])
    return _step("skill_lint", rc == 0, out.strip()[:160] or "rc=%d" % rc)


def step_question_lint(tmp: Path) -> dict:
    req = tmp / "req.json"
    req.write_text(
        json.dumps(
            {
                "questions": {
                    "q": {
                        "type": "noul",
                        "instructions": "Should the coder proceed with the plan?",
                        "criteria": {"true": "plan is sound", "false": "plan is risky"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    rc, out = _run([str(SCRIPTS / "question_lint.py"), str(req)])
    ok = rc == 0 and "lint:" in out
    return _step("question_lint", ok, out.strip().splitlines()[-1][:120] if out.strip() else "rc=%d" % rc)


def step_apply_fill(tmp: Path) -> dict:
    env = dict(os.environ)
    env["JEV_CONSULT_LOG"] = "0"
    env.pop("TYPESAFE_API_KEY", None)
    env["USERPROFILE"] = str(tmp / "home")
    env["HOME"] = str(tmp / "home")
    rc, out = _run(
        [
            str(SCRIPTS / "apply_fill.py"),
            "--task",
            "jwt",
            "--harness",
            "claude-code",
            "--cwd",
            str(tmp / "cwd"),
            "--dry-run",
        ],
        env=env,
    )
    ok = rc == 0 and "human" in out
    return _step("apply_fill", ok, out.strip()[:120] or "rc=%d" % rc)


def step_hook(tmp: Path) -> dict:
    env = dict(os.environ)
    env["JEV_CONSULT_LOG"] = "0"
    env.pop("TYPESAFE_API_KEY", None)
    env["USERPROFILE"] = str(tmp / "home")
    env["HOME"] = str(tmp / "home")
    payload = json.dumps(
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "smoke test task",
            "cwd": str(tmp / "cwd"),
        }
    )
    rc, out = _run(
        [str(SCRIPTS / "inventory_hook.py")], cwd=tmp, env=env, inp=payload
    )
    ok = False
    if rc == 0:
        try:
            ok = isinstance(json.loads(out.strip().splitlines()[0]), dict)
        except (ValueError, IndexError):
            ok = False
    return _step("hook", ok, out.strip()[:120] or "rc=%d" % rc)


def step_compare(tmp: Path) -> dict:
    rc, out = _run([str(SCRIPTS / "compare.py"), "--strict"])
    ok = rc == 0 and "after_jev" in out
    return _step("compare", ok, out.strip().splitlines()[-1][:120] if out.strip() else "rc=%d" % rc)


def step_doctor(tmp: Path) -> dict:
    rc, out = _run(
        [
            str(SCRIPTS / "doctor.py"),
            "--agents",
            "hermes",
            "--home",
            str(tmp / "home"),
            "--hermes-home",
            str(tmp / "hermes"),
        ]
    )
    try:
        ok_json = isinstance(json.loads(out), dict)
    except ValueError:
        ok_json = False
    return _step("doctor_json", ok_json and rc in (0, 1), "rc=%d json=%s" % (rc, ok_json))


STEPS = (
    ("policy", "step_policy"),
    ("policy_lint", "step_policy_lint"),
    ("jev_scaffold_lint", "step_jev_scaffold_lint"),
    ("inventory", "step_inventory"),
    ("compact_fake", "step_compact_fake"),
    ("decisions", "step_decisions"),
    ("trace", "step_trace"),
    ("skill_lint", "step_skill_lint"),
    ("question_lint", "step_question_lint"),
    ("compare", "step_compare"),
    ("apply_fill", "step_apply_fill"),
    ("hook", "step_hook"),
    ("doctor_json", "step_doctor"),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline sanity for the jev-consult pack.")
    parser.add_argument(
        "--only",
        default=os.environ.get("JEV_SMOKE_ONLY", ""),
        help="Comma-separated step names to run (default: all; JEV_SMOKE_ONLY presets).",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first failing step.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print step names (for --only) and exit.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="With --list, emit the step names as a JSON array.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Per-step subprocess timeout (default 60; JEV_SMOKE_TIMEOUT overrides).",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        default="",
        help="Also write the results JSON to PATH.",
    )
    parser.add_argument(
        "--watch",
        metavar="S",
        type=float,
        default=0.0,
        help="Re-run the steps every S seconds, printing a {ts,ok,failed} JSON tick.",
    )
    parser.add_argument("--max-ticks", metavar="N", type=int, default=0, help="With --watch: stop after N ticks (overrides the JEV_*_WATCH_MAX env)")
    args = parser.parse_args(argv)
    names = {name for name, _ in STEPS}
    if args.list:
        if getattr(args, "json", False):
            sys.stdout.write(json.dumps(sorted(names)) + "\n")
        else:
            for name in sorted(names):
                sys.stdout.write(name + "\n")
        return 0
    wanted = {s.strip() for s in args.only.split(",") if s.strip()}
    unknown = wanted - names
    if unknown:
        sys.stderr.write(
            "unknown step(s): %s (valid: %s)\n" % (", ".join(sorted(unknown)), ", ".join(sorted(names)))
        )
        return 2
    global STEP_TIMEOUT
    if args.timeout > 0:
        STEP_TIMEOUT = float(args.timeout)
    else:
        try:
            env_timeout = float(os.environ.get("JEV_SMOKE_TIMEOUT", "") or "0")
        except ValueError:
            env_timeout = 0.0
        if env_timeout > 0:
            STEP_TIMEOUT = env_timeout
    def _run_steps() -> list[dict]:
        rows: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            for name, fn_name in STEPS:
                if wanted and name not in wanted:
                    continue
                fn = globals()[fn_name]
                try:
                    rows.append(fn(tmp))
                except Exception as exc:  # a crash is a failed step, not a crash
                    rows.append(_step(getattr(fn, "__name__", "step"), False, "raised %r" % exc))
                if args.fail_fast and not rows[-1]["ok"]:
                    break
        return rows

    if args.watch and args.watch > 0:
        import time as _time

        max_ticks = _watch.cap("JEV_SMOKE_WATCH_MAX", args.max_ticks)
        ticks = 0
        while max_ticks <= 0 or ticks < max_ticks:
            steps = _run_steps()
            tick = {
                "ts": int(_time.time()),
                "ok": all(s["ok"] for s in steps),
                "failed": [s["name"] for s in steps if not s["ok"]],
            }
            _watch.emit(tick, args.out)
            ticks += 1
            _time.sleep(args.watch)
        return 0 if tick["ok"] else 1
    steps = _run_steps()
    ok = all(s["ok"] for s in steps)
    text = json.dumps({"ok": ok, "steps": steps}, indent=2) + "\n"
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

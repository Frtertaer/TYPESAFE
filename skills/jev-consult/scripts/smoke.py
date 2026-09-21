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
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS.parent


def _step(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _run(argv: list[str], cwd: Path | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, *argv],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=60,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline sanity for the jev-consult pack.")
    parser.parse_args(argv)
    steps: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp_raw:
        tmp = Path(tmp_raw)
        for fn in (
            step_policy,
            step_policy_lint,
            step_jev_scaffold_lint,
            step_inventory,
            step_compact_fake,
            step_decisions,
            step_trace,
            step_skill_lint,
            step_question_lint,
            step_doctor,
        ):
            try:
                steps.append(fn(tmp))
            except Exception as exc:  # a crash is a failed step, not a crash
                steps.append(_step(getattr(fn, "__name__", "step"), False, "raised %r" % exc))
    ok = all(s["ok"] for s in steps)
    sys.stdout.write(json.dumps({"ok": ok, "steps": steps}, indent=2) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

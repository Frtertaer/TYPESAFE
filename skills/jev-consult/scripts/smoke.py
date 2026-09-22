#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""smoke.py - offline end-to-end sanity for the jev-consult pack.

Runs each script's offline path in a temp dir: policy loads + policy_lint,
jev scaffold + lint (no network ask), inventory scan, compact --fake,
decisions.py stats over a fixture log, trace.py init + state, doctor,
and jev.py ask --verdict against a one-shot 127.0.0.1 stub endpoint
(never the real Jev API; the stub key is a literal dummy). Prints JSON
{ok, steps:[{name, ok, detail}]}; exit 0/1. Safe for CI.
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
from xml.sax.saxutils import escape  # noqa: E402

import http.server  # noqa: E402
import threading  # noqa: E402

SKILL_DIR = SCRIPTS.parent


def jq_lookup(obj, path: str):
    """Dotted-path lookup; (value, True) or (None, False) when any part misses."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None, False
    return cur, True


def junit_xml(steps: list[dict]) -> str:
    """Render a JUnit <testsuite> document for the step rows."""
    failures = sum(1 for s in steps if not s.get("ok"))
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<testsuite name="jev-smoke" tests="%d" failures="%d">'
        % (len(steps), failures),
    ]
    for s in steps:
        name = escape(str(s.get("name") or "step"), {'"': "&quot;"})
        lines.append(
            '  <testcase name="%s" classname="jev-consult.smoke">' % name
        )
        if not s.get("ok"):
            detail = escape(str(s.get("detail") or "failed"))
            lines.append('    <failure>%s</failure>' % detail)
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    return "\n".join(lines) + "\n"


def _step(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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
    if ok:
        sidecar = tmp / "sc.json"
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
                "--sidecar",
                str(sidecar),
            ]
        )
        ok = rc == 0 and sidecar.is_file()
    if ok:
        rc, out = _run(
            [str(SCRIPTS / "inventory.py"), "--check-sidecar", str(sidecar)]
        )
        ok = rc == 0 and "fresh" in out
    if ok:
        rc, out = _run([str(SCRIPTS / "inventory.py"), "--show", str(sidecar)])
        if rc == 0:
            try:
                ok = json.loads(out).get("status") == "fresh"
            except ValueError:
                ok = False
        else:
            ok = False
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
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--statuses",
                "--json",
            ]
        )
        if rc == 0:
            try:
                ok = json.loads(out).get("counts", {}).get("winner") == 1
            except (ValueError, AttributeError):
                ok = False
        else:
            ok = False
    if ok:
        bad_log = tmp / "decisions-bad.jsonl"
        bad_log.write_text(
            "not json\n" + json.dumps({"harness": "smoke"}) + "\n",
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(bad_log),
                "--errors",
                "--json",
            ]
        )
        try:
            ok = rc == 1 and len(json.loads(out)) == 1
        except ValueError:
            ok = False
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(bad_log),
                "--validate",
                "--json",
            ]
        )
        try:
            rows = json.loads(out)
            ok = rc == 1 and rows and rows[0]["missing"] == [
                "ts",
                "jev_status",
            ]
        except (ValueError, IndexError, KeyError):
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
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "trace.py"),
                "--file",
                str(trace_file),
                "record",
                "--pick",
                "smoke-pick",
                "--note",
                "smoke note",
            ]
        )
        ok = rc == 0
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "trace.py"),
                "--file",
                str(trace_file),
                "notes",
                "--json",
            ]
        )
        if rc == 0:
            try:
                ok = any(
                    "smoke note" in str(n.get("text", ""))
                    for n in json.loads(out)
                )
            except (ValueError, AttributeError):
                ok = False
        else:
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
    env = dict(os.environ)  # skillscan:allow
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
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "apply_fill.py"),
                "--status",
                "--cwd",
                str(tmp / "cwd"),
            ],
            env=env,
        )
        if rc == 0:
            try:
                report = json.loads(out)
                ok = "miss" in report and "ask" in report
            except (ValueError, AttributeError):
                ok = False
        else:
            ok = False
    return _step("apply_fill", ok, out.strip()[:120] or "rc=%d" % rc)


def step_hook(tmp: Path) -> dict:
    env = dict(os.environ)  # skillscan:allow
    env["JEV_CONSULT_LOG"] = "0"
    env.pop("TYPESAFE_API_KEY", None)
    env["USERPROFILE"] = str(tmp / "home")
    env["HOME"] = str(tmp / "home")
    cwd = tmp / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "smoke test task",
            "cwd": str(cwd),
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
    if ok:
        # no Jev key + prompt tokens => the hook records a miss marker
        ok = (cwd / ".jev-tools-miss.json").is_file()
    if ok:
        # repeat of the same prompt over a fresh sidecar hits the dedupe path
        env["JEV_HOOK_DEBUG"] = "1"
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py")], cwd=tmp, env=env, inp=payload
        )
        ok = rc == 0 and "dedupe=True" in out
    if ok:
        # --verbose explains a {} emit on stderr
        env.pop("JEV_HOOK_DEBUG", None)
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py"), "--verbose"],
            cwd=tmp,
            env=env,
            inp=json.dumps({"hook_event_name": "PreToolUse", "prompt": "x"}),
        )
        ok = rc == 0 and "verbose:" in out
    if ok:
        # same prompt over an expired sidecar hits the stale_match path;
        # --json echoes the whole LAST_DECISION record to stderr
        sidecar = json.loads((cwd / ".jev-tools.json").read_text(encoding="utf-8"))
        sidecar["written_at"] = 1  # ancient -> stale
        (cwd / ".jev-tools.json").write_text(
            json.dumps(sidecar), encoding="utf-8"
        )
        env.pop("JEV_HOOK_DEBUG", None)
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py"), "--json"],
            cwd=tmp,
            env=env,
            inp=payload,
        )
        ok = rc == 0 and '"stale_sidecar": true' in out
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
            "hermes,claude-code,codex,grok",
            "--home",
            str(tmp / "home"),
            "--hermes-home",
            str(tmp / "hermes"),
        ]
    )
    try:
        payload = json.loads(out)
        agents_seen = {
            c.get("agent") for c in payload.get("checks", []) if c.get("agent")
        }
        ok_json = isinstance(payload, dict) and {
            "hermes",
            "claude-code",
            "codex",
            "grok",
        } <= agents_seen
    except ValueError:
        ok_json = False
    return _step("doctor_json", ok_json and rc in (0, 1), "rc=%d json=%s" % (rc, ok_json))


def step_trigger_lint(tmp: Path) -> dict:
    cases = SCRIPTS.parent.parent.parent / "tests" / "fixtures" / "jev-consult.trigger-cases.json"
    if not cases.is_file():
        return _step("trigger_lint", False, "fixture missing: %s" % cases)
    rc, out = _run([str(SCRIPTS / "trigger_lint.py"), str(cases)])
    return _step("trigger_lint", rc == 0, out.strip()[:120] or "rc=%d" % rc)


def step_trigger_eval(tmp: Path) -> dict:
    rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--quiet"])
    ok = rc == 0 and "PASS" in out
    return _step("trigger_eval", ok, out.strip()[:120] or "rc=%d" % rc)


def step_compact_hook(tmp: Path) -> dict:
    env = dict(os.environ)  # skillscan:allow
    env.pop("TYPESAFE_API_KEY", None)
    env["USERPROFILE"] = str(tmp / "home")
    env["HOME"] = str(tmp / "home")
    spill = tmp / "spill"
    env["JEV_CONSULT_SPILL"] = str(spill)
    payload = json.dumps(
        {"hook_event_name": "PostToolUse", "tool_result": "x" * 90000}
    )
    rc, out = _run([str(SCRIPTS / "compact_hook.py")], cwd=tmp, env=env, inp=payload)
    ok = False
    if rc == 0:
        try:
            ok = isinstance(json.loads(out.strip().splitlines()[0]), dict)
        except (ValueError, IndexError):
            ok = False
    if ok:
        # lossless spill: the 90k result leaves a full copy on disk
        try:
            ok = any(spill.iterdir())
        except OSError:
            ok = False
    return _step("compact_hook", ok, out.strip()[:120] or "rc=%d" % rc)


def step_jev_decide(tmp: Path) -> dict:
    answers = tmp / "answers.json"
    answers.write_text(
        json.dumps(
            {
                "answers": {
                    "where": {
                        "type": "choice",
                        "choice": "refactor",
                        "confidence": 0.9,
                        "probabilities": {"refactor": 0.9, "rewrite": 0.1},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    rc, out = _run(
        [
            str(SCRIPTS / "jev.py"),
            "decide",
            str(answers),
            "--jq",
            "decision.action",
        ]
    )
    ok = rc == 0 and out.strip() == '"proceed"'
    if ok:
        verdict = tmp / "decide-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "jev.py"),
                "decide",
                str(answers),
                "--verdict",
                str(verdict),
            ]
        )
        try:
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            ok = rc == 0 and payload.get("verdict") == "proceed"
        except (OSError, ValueError):
            ok = False
    return _step("jev_decide", ok, out.strip()[:120] or "rc=%d" % rc)


def step_ask_verdict(tmp: Path) -> dict:
    """jev.py ask against a one-shot localhost stub (never the real API)."""
    body = json.dumps(
        {
            "answers": {
                "q": {
                    "type": "choice",
                    "choice": "a",
                    "confidence": 0.9,
                    "probabilities": {"a": 0.9, "b": 0.1},
                }
            }
        }
    ).encode("utf-8")

    class Stub(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        policy = json.loads((SKILL_DIR / "policy.json").read_text(encoding="utf-8"))
        policy["endpoint"] = "http://127.0.0.1:%d/v1/systemone" % port
        policy_path = tmp / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        request = tmp / "ask.json"
        request.write_text(
            json.dumps(
                {
                    "state": "smoke test",
                    "questions": {
                        "q": {
                            "type": "choice",
                            "instructions": "pick one",
                            "criteria": {"a": "first", "b": "second"},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        env = dict(os.environ)  # skillscan:allow
        env["JEV_POLICY"] = str(policy_path)
        env["TYPESAFE_API_KEY"] = "smoke-stub-key"
        verdict = tmp / "verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "jev.py"),
                "ask",
                str(request),
                "--verdict",
                str(verdict),
            ],
            env=env,
        )
        ok = False
        if rc == 0 and verdict.is_file():
            try:
                ok = json.loads(verdict.read_text(encoding="utf-8")).get(
                    "verdict"
                ) == "proceed"
            except ValueError:
                ok = False
        return _step("ask_verdict", ok, out.strip()[:120] or "rc=%d" % rc)
    finally:
        server.server_close()


def step_peer_fill_status(tmp: Path) -> dict:
    rc, out = _run(
        [
            str(SCRIPTS / "peer_fill.py"),
            "--status",
            "--cwd",
            str(tmp / "cwd"),
        ]
    )
    ok = False
    if rc == 0:
        try:
            ok = isinstance(json.loads(out), dict)
        except ValueError:
            ok = False
    return _step("peer_fill_status", ok, out.strip()[:120] or "rc=%d" % rc)


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
    ("trigger_lint", "step_trigger_lint"),
    ("trigger_eval", "step_trigger_eval"),
    ("compact_hook", "step_compact_hook"),
    ("jev_decide", "step_jev_decide"),
    ("peer_fill_status", "step_peer_fill_status"),
    ("ask_verdict", "step_ask_verdict"),
)


def main(argv: list[str] | None = None) -> int:
    if _watch.maybe_version(sys.argv[1:] if argv is None else argv):
        return 0
    parser = argparse.ArgumentParser(description="Offline sanity for the jev-consult pack.")
    parser.add_argument(
        "--only",
        default=os.environ.get("JEV_SMOKE_ONLY", ""),
        help="Comma-separated step names to run (default: all; JEV_SMOKE_ONLY presets).",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first failing step (in --watch mode, stops on the first failing tick).",
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
        "--report",
        metavar="PATH",
        default="",
        help="Write a markdown step report (verdict + per-step table) to PATH.",
    )
    parser.add_argument(
        "--watch",
        metavar="S",
        type=float,
        default=0.0,
        help="Re-run the steps every S seconds, printing a {ts,ok,failed} JSON tick.",
    )
    parser.add_argument("--max-ticks", metavar="N", type=int, default=0, help="With --watch: stop after N ticks (overrides the JEV_*_WATCH_MAX env)")
    parser.add_argument("--watch-max", metavar="S", type=float, default=0.0, help="With --watch: stop after S elapsed seconds")
    parser.add_argument("--quiet", action="store_true", help="With --watch: print only failing ticks to stdout (--out still logs all)")
    parser.add_argument("--verdict", metavar="PATH", default="", help="Write a slim {verdict, steps, failed} JSON to PATH when finished (in --watch mode, the final pass state).")
    parser.add_argument("--junit", metavar="PATH", default="", help="Write a JUnit XML <testsuite> for the step results to PATH (in --watch mode, the final pass).")
    parser.add_argument(
        "--repeat",
        metavar="N",
        type=int,
        default=0,
        help="Run each step N times; a step fails when any attempt does (default 1; JEV_SMOKE_REPEAT presets).",
    )
    parser.add_argument(
        "--jq",
        metavar="KEY",
        default="",
        help="Print just one dotted-path field of the results payload (e.g. ok); unknown key exits 2.",
    )
    args = parser.parse_args(argv)
    names = {name for name, _ in STEPS}
    if args.list:
        if getattr(args, "json", False):
            sys.stdout.write(json.dumps(sorted(names)) + "\n")
        else:
            for name in sorted(names):
                sys.stdout.write(name + "\n")
        return 0
    repeat = args.repeat
    if repeat <= 0:
        try:
            repeat = int(os.environ.get("JEV_SMOKE_REPEAT", "") or "1")
        except ValueError:
            repeat = 1
    if repeat < 1:
        repeat = 1
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
                for attempt in range(repeat):
                    try:
                        row = fn(tmp)
                    except Exception as exc:  # a crash is a failed step, not a crash
                        row = _step(getattr(fn, "__name__", "step"), False, "raised %r" % exc)
                    if not row["ok"] and repeat > 1:
                        row = dict(row)
                        row["detail"] = "attempt %d/%d: %s" % (
                            attempt + 1, repeat, row["detail"]
                        )
                    if not row["ok"]:
                        break
                rows.append(row)
                if args.fail_fast and not rows[-1]["ok"]:
                    break
        return rows

    def _write_verdict(steps_now: list[dict], elapsed_s=None) -> bool:
        if not args.verdict:
            return True
        payload = {
            "verdict": "PASS" if all(s["ok"] for s in steps_now) else "FAIL",
            "steps": len(steps_now),
            "failed": [s["name"] for s in steps_now if not s["ok"]],
        }
        if elapsed_s is not None:
            payload["elapsed_s"] = elapsed_s
        return _watch.write_verdict(args.verdict, payload)

    if args.watch and args.watch > 0:
        import time as _time

        max_ticks = _watch.cap("JEV_SMOKE_WATCH_MAX", args.max_ticks)
        ticks = 0
        dead = _watch.deadline("JEV_SMOKE_WATCH_SECS", getattr(args, "watch_max", 0.0))
        last_steps: list[dict] = []
        verdict_ok = True
        watch_t0 = _time.time()
        while (max_ticks <= 0 or ticks < max_ticks) and (not dead or _time.time() < dead):
            steps = _run_steps()
            tick = {
                "ts": int(_time.time()),
                "ok": all(s["ok"] for s in steps),
                "failed": [s["name"] for s in steps if not s["ok"]],
                "elapsed_s": round(_time.time() - watch_t0, 2),
            }
            _watch.emit_or_jq(tick, args.jq, args.out, quiet=_watch.quiet("JEV_SMOKE_WATCH_QUIET", args.quiet), bad=bool(tick["failed"]))
            last_steps = steps
            ticks += 1
            sys.stderr.write(
                "watch tick=%d ok=%s failed=%s\n"
                % (ticks, tick["ok"], ",".join(tick["failed"]) or "-")
            )
            if verdict_ok and not _write_verdict(
                last_steps, elapsed_s=round(_time.time() - watch_t0, 2)
            ):
                verdict_ok = False  # warn once, stop retrying
            if args.fail_fast and tick["failed"]:
                break
            _time.sleep(args.watch)
        if args.verdict and verdict_ok and not _write_verdict(
            last_steps, elapsed_s=round(_time.time() - watch_t0, 2)
        ):
            return 1
        if args.junit:
            try:
                _atomic_write(Path(args.junit), junit_xml(last_steps))
            except OSError as exc:
                sys.stderr.write("cannot write %s: %s\n" % (args.junit, exc))
                return 1
        return 0 if tick["ok"] else 1
    steps = _run_steps()
    ok = all(s["ok"] for s in steps)
    text = json.dumps({"ok": ok, "steps": steps}, indent=2) + "\n"
    if args.jq:
        value, found = jq_lookup({"ok": ok, "steps": steps}, args.jq)
        if not found:
            sys.stderr.write(
                "bad --jq key %r (payload has: ok, steps)\n" % args.jq
            )
            return 2
        sys.stdout.write(json.dumps(value) + "\n")
    else:
        sys.stdout.write(text)
    if args.out:
        try:
            _atomic_write(Path(args.out), text)
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (args.out, exc))
            return 1
        sys.stderr.write("wrote %s\n" % args.out)
    if args.verdict and not _write_verdict(steps):
        return 1
    if args.report:
        lines = [
            "# smoke report",
            "",
            "verdict: **%s**" % ("PASS" if ok else "FAIL"),
            "",
            "- steps: %d" % len(steps),
            "- failed: %d" % sum(1 for s in steps if not s["ok"]),
            "",
            "| step | ok | detail |",
            "| --- | --- | --- |",
        ]
        for s in steps:
            detail = str(s.get("detail") or "").replace("|", "\\|").replace("\n", " ")
            lines.append(
                "| %s | %s | %s |" % (s["name"], "yes" if s["ok"] else "NO", detail)
            )
        try:
            _atomic_write(Path(args.report), "\n".join(lines) + "\n")
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (args.report, exc))
            return 1
        sys.stderr.write("wrote %s\n" % args.report)
    if args.junit:
        try:
            _atomic_write(Path(args.junit), junit_xml(steps))
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (args.junit, exc))
            return 1
        sys.stderr.write("wrote %s\n" % args.junit)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

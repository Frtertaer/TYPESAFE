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
import time
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
    ok = rc == 0
    if ok:
        # --fix --dry-run reports without writing; --fix drops the key
        bad = tmp / "policy-bad.json"
        policy = json.loads(
            (SCRIPTS.parent / "policy.json").read_text(encoding="utf-8")
        )
        policy["smoke_bogus_key"] = 1
        bad.write_text(json.dumps(policy), encoding="utf-8")
        rc, out = _run(
            [
                str(SCRIPTS / "policy_lint.py"),
                str(bad),
                "--fix",
                "--dry-run",
            ]
        )
        ok = (
            rc == 0
            and "would fix P011" in out
            and '"smoke_bogus_key"' in bad.read_text(encoding="utf-8")
        )
    if ok:
        rc, out = _run(
            [str(SCRIPTS / "policy_lint.py"), str(bad), "--fix"]
        )
        ok = (
            rc == 0
            and "fixed P011" in out
            and "smoke_bogus_key" not in bad.read_text(encoding="utf-8")
        )
    if ok:
        # --json emits a findings payload on the shipped policy
        rc, out = _run([str(SCRIPTS / "policy_lint.py"), "--json"])
        try:
            ok = rc == 0 and isinstance(json.loads(out).get("findings"), list)
        except (ValueError, AttributeError):
            ok = False
    if ok:
        # --explain RULE prints one rule's description
        rc, out = _run(
            [str(SCRIPTS / "policy_lint.py"), "--explain", "P001"]
        )
        ok = rc == 0 and "P001" in out
    if ok:
        # --watch emits {findings,errors} ticks; --verdict writes the probe
        verdict = tmp / "plint-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "policy_lint.py"),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--verdict",
                str(verdict),
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = rc in (0, 1) and len(ticks) == 2
        try:
            ok = ok and "verdict" in json.loads(
                verdict.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            ok = False
    if ok:
        # --fail-fast stops the watch on the first erroring tick
        empty_policy = tmp / "policy-empty.json"
        empty_policy.write_text("{}", encoding="utf-8")
        rc, out = _run(
            [
                str(SCRIPTS / "policy_lint.py"),
                str(empty_policy),
                "--watch",
                "0.03",
                "--max-ticks",
                "5",
                "--fail-fast",
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = len(ticks) == 1 and "watch tick=2" not in out
    return _step("policy_lint", ok, out.strip().splitlines()[-1][:120] if out.strip() else "rc=%d" % rc)


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
    ok = rc == 0
    if ok:
        # a choice template with empty criteria and no --option fails
        empty_policy = tmp / "policy-empty.json"
        policy = json.loads(
            (SKILL_DIR / "policy.json").read_text(encoding="utf-8")
        )
        policy["templates"] = {
            "empty_t": {
                "type": "choice",
                "instructions": "pick",
                "criteria": {},
            }
        }
        empty_policy.write_text(json.dumps(policy), encoding="utf-8")
        rc, out = _run(
            [
                str(SCRIPTS / "jev.py"),
                "--policy",
                str(empty_policy),
                "scaffold",
                "empty_t",
                "--out",
                str(tmp / "empty.json"),
            ]
        )
        ok = rc == 2 and "empty template criteria" in out
    if ok:
        # ask --dry resolves the request locally (no API call, no key)
        env = dict(os.environ)  # skillscan:allow
        env.pop("TYPESAFE_API_KEY", None)
        rc, out = _run(
            [str(SCRIPTS / "jev.py"), "ask", str(req), "--dry"], env=env
        )
        if rc == 0:
            try:
                ok = "questions" in json.loads(out.strip())
            except ValueError:
                ok = False
        else:
            ok = False
    return _step("jev_scaffold_lint", ok, out.strip()[:160] or "clean")


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
        # --diff OLD.json compares the current scan against a saved payload
        old_payload = tmp / "inv-old.json"
        old_payload.write_text(out, encoding="utf-8")
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
                "--diff",
                str(old_payload),
            ]
        )
        if rc == 0:
            try:
                diff = json.loads(out)
                ok = diff.get("added") == [] and diff.get("removed") == []
            except (ValueError, AttributeError):
                ok = False
        else:
            ok = False
    if ok:
        # --verdict PATH writes the slim one-shot probe JSON
        verdict = tmp / "inv-verdict.json"
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
                "--verdict",
                str(verdict),
            ]
        )
        try:
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            ok = rc == 0 and payload.get("verdict") == "ok"
        except (OSError, ValueError):
            ok = False
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
        # backdating written_at flips the sidecar to stale
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            payload["written_at"] = time.time() - 10 * 365 * 86400
            sidecar.write_text(json.dumps(payload), encoding="utf-8")
        except (OSError, ValueError):
            ok = False
    if ok:
        rc, out = _run(
            [str(SCRIPTS / "inventory.py"), "--check-sidecar", str(sidecar)]
        )
        ok = rc == 0 and "stale" in out
    if ok:
        rc, out = _run([str(SCRIPTS / "inventory.py"), "--show", str(sidecar)])
        if rc == 0:
            try:
                ok = json.loads(out).get("status") == "stale"
            except ValueError:
                ok = False
        else:
            ok = False
    if ok:
        # a scan with nothing installed reports an empty verdict
        empty_home = tmp / "empty-home"
        empty_home.mkdir(exist_ok=True)
        empty_verdict = tmp / "inv-empty-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "inventory.py"),
                "--harness",
                "codex",
                "--home",
                str(empty_home),
                "--hermes-home",
                str(empty_home / "hermes"),
                "--task",
                "smoke",
                "--verdict",
                str(empty_verdict),
            ]
        )
        try:
            payload = json.loads(empty_verdict.read_text(encoding="utf-8"))
            ok = rc == 0 and payload.get("verdict") == "empty"
        except (OSError, ValueError):
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
    if ok:
        # --check exits 1 when the transcript compacts below the gate
        rc, out = _run(
            [
                str(SCRIPTS / "compact.py"),
                str(transcript),
                "--history",
                "--fake",
                "--check",
                "--min-reduction",
                "50",
            ]
        )
        ok = rc == 1 and "check: FAIL" in out
    if ok:
        # --report writes the stats dict as JSON to PATH
        report = tmp / "compact-report.json"
        rc, out = _run(
            [
                str(SCRIPTS / "compact.py"),
                str(transcript),
                "--history",
                "--fake",
                "--min-reduction",
                "0",
                "--report",
                str(report),
            ]
        )
        try:
            stats = json.loads(report.read_text(encoding="utf-8"))
            ok = rc == 0 and isinstance(stats, dict) and bool(stats)
        except (OSError, ValueError):
            ok = False
    if ok:
        # --watch S --max-ticks N re-scores the transcript each tick
        rc, out = _run(
            [
                str(SCRIPTS / "compact.py"),
                str(transcript),
                "--history",
                "--fake",
                "--watch",
                "0.05",
                "--max-ticks",
                "2",
            ]
        )
        # exits 1 when the last tick fell back (already-compact transcript)
        ok = rc in (0, 1) and out.count("watch tick=") == 2
    if ok:
        # --stats prints a one-line summary; --stats-json the stats dict
        # (both on stderr, after the result JSON)
        rc, out = _run(
            [
                str(SCRIPTS / "compact.py"),
                str(transcript),
                "--history",
                "--fake",
                "--min-reduction",
                "0",
                "--stats",
            ]
        )
        ok = rc == 0 and "stats:" in out
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "compact.py"),
                str(transcript),
                "--history",
                "--fake",
                "--min-reduction",
                "0",
                "--stats-json",
            ]
        )
        ok = rc == 0 and '"charsBefore"' in out
    if ok:
        # --explain logs one decision line per tool call on stderr
        rc, out = _run(
            [
                str(SCRIPTS / "compact.py"),
                str(transcript),
                "--history",
                "--fake",
                "--min-reduction",
                "0",
                "--explain",
            ]
        )
        ok = rc == 0
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
    if ok:
        # --drop-bad --dry-run reports without rewriting; --drop-bad applies
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(bad_log),
                "--drop-bad",
                "--dry-run",
            ]
        )
        ok = (
            rc == 0
            and "would drop 1" in out
            and len(bad_log.read_text(encoding="utf-8").splitlines()) == 2
        )
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(bad_log),
                "--drop-bad",
            ]
        )
        try:
            lines = bad_log.read_text(encoding="utf-8").splitlines()
            ok = (
                rc == 0
                and "dropped 1" in out
                and len(lines) == 1
                and json.loads(lines[0]).get("harness") == "smoke"
            )
        except (ValueError, IndexError):
            ok = False
    if ok:
        # --prune --dry-run reports counts without rewriting; --prune applies
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--status",
                "winner",
                "--prune",
                "--dry-run",
                "--json",
            ]
        )
        try:
            summary = json.loads(out).get("prune_dry_run", {})
            ok = (
                rc == 0
                and summary.get("kept") == 1
                and summary.get("would_prune") == 1
                and len(log.read_text(encoding="utf-8").splitlines()) == 2
            )
        except (ValueError, AttributeError):
            ok = False
    if ok:
        # --tail 1 lists only the last entry (a "none" row)
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--tail",
                "1",
            ]
        )
        ok = rc == 0 and "none" in out.splitlines()[-1] and "winner" not in out.splitlines()[-1]
    if ok:
        # --first 1 lists only the earliest entry (a "winner" row)
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--first",
                "1",
            ]
        )
        ok = rc == 0 and "winner" in out.splitlines()[-1] and "none" not in out.splitlines()[-1]
    if ok:
        # --reverse flips the listed rows (the "none" row leads)
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--first",
                "2",
                "--reverse",
            ]
        )
        ok = rc == 0 and "none" in out.splitlines()[-2] and "winner" in out.splitlines()[-1]
    if ok:
        # --last prints the newest matching entry as JSON
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--last",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("jev_status") == "none"
        except ValueError:
            ok = False
    if ok:
        # --oldest prints the earliest matching entry as JSON
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--oldest",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("jev_status") == "winner"
        except ValueError:
            ok = False
    if ok:
        # --nth 1 prints the first matching entry as JSON
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--nth",
                "1",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("jev_status") == "winner"
        except ValueError:
            ok = False
    if ok:
        # --field KEY=VALUE filters on any entry field
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--field",
                "jev_status=none",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --grep substring-filters across string fields
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--grep",
                "winner",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --status filters on jev_status
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--status",
                "winner",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --harness filters on the harness field
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--harness",
                "smoke",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 2
        except ValueError:
            ok = False
    if ok:
        # --outcome filters on the fill outcome field
        outcome_log = tmp / "decisions-outcome.jsonl"
        outcome_log.write_text(
            '\n'.join(
                json.dumps({"ts": 1, "jev_status": "fill", "outcome": s})
                for s in ("human", "blocked")
            )
            + "\n",
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(outcome_log),
                "--outcome",
                "human",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --min-need filters on the numeric need field
        need_log = tmp / "decisions-need.jsonl"
        need_log.write_text(
            '\n'.join(
                json.dumps({"ts": 1, "jev_status": "winner", "need": n})
                for n in (0.2, 0.9)
            )
            + "\n",
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(need_log),
                "--min-need",
                "0.5",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --where KEY=VAL filters by field equality (case-insensitive)
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--where",
                "jev_status=WINNER",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --winner/--winners/--explicit filter on winner name + explicit flag
        winner_log = tmp / "decisions-winners.jsonl"
        winner_log.write_text(
            '\n'.join(
                [
                    json.dumps({"ts": 1, "jev_status": "winner", "winner": {"kind": "skill", "name": "alpha"}, "explicit": True, "question": "explicit"}),
                    json.dumps({"ts": 2, "jev_status": "winner", "winner": {"kind": "skill", "name": "beta"}}),
                    json.dumps({"ts": 3, "jev_status": "none"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(winner_log),
                "--winner",
                "alpha",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(winner_log),
                "--explicit",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(winner_log),
                "--winners",
            ]
        )
        ok = rc == 0 and "skill:alpha" in out and "skill:beta" in out
    if ok:
        # --missing FIELD keeps only entries lacking the field
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(winner_log),
                "--missing",
                "winner",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --jq KEY --jq-where-contains SUB keeps only matching values
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(winner_log),
                "--jq",
                "winner.name",
                "--jq-where-contains",
                "alp",
            ]
        )
        ok = rc == 0 and out.strip() == "alpha"
    if ok:
        # --since/--until bound entries by ts window
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(winner_log),
                "--since",
                "2",
                "--until",
                "2",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --daily buckets counts by UTC day
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--daily",
            ]
        )
        ok = rc == 0 and "2023-11-14" in out
    if ok:
        # --group-by FIELD counts entries per field value
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--group-by",
                "jev_status",
            ]
        )
        ok = rc == 0 and "winner" in out and "none" in out
    if ok:
        # --count prints only the matching entry count
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--count",
            ]
        )
        ok = rc == 0 and out.strip() == "2"
    if ok:
        # --question KIND filters by the routing question kind
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(winner_log),
                "--question",
                "explicit",
                "--json",
            ]
        )
        # only the explicit-pick row carries question="explicit"
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --uniq dedupes --jq values
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--jq",
                "harness",
                "--uniq",
            ]
        )
        ok = rc == 0 and out.strip() == "smoke"
    if ok:
        # --fields lists field names with counts
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--fields",
            ]
        )
        ok = rc == 0 and "jev_status" in out and "harness" in out
    if ok:
        # count lists: --harnesses/--outcomes/--fills/--dedupes group a field
        kind_log = tmp / "decisions-kinds.jsonl"
        kind_log.write_text(
            '\n'.join(
                [
                    json.dumps({"ts": 1, "jev_status": "winner", "harness": "hermes", "outcome": "human", "fill": "trace", "dedupe": True}),
                    json.dumps({"ts": 2, "jev_status": "none", "harness": "codex", "outcome": "auto", "fill": "peer", "dedupe": False}),
                    json.dumps({"ts": 3, "jev_status": "winner", "harness": "hermes", "outcome": "human", "fill": "trace", "dedupe": True}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        counts_ok = True
        for flag, want in (
            ("--harnesses", "hermes 2"),
            ("--outcomes", "human 2"),
            ("--fills", "trace 2"),
            ("--dedupes", "True 2"),
        ):
            rc, out = _run(
                [str(SCRIPTS / "decisions.py"), "--file", str(kind_log), flag]
            )
            if not (rc == 0 and want in out):
                counts_ok = False
        ok = counts_ok
    if ok:
        # --top N caps count-list rows
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(kind_log),
                "--harnesses",
                "--top",
                "1",
            ]
        )
        ok = rc == 0 and out.strip() == "hermes 2"
    if ok:
        # boolean-flag filters keep only entries with the flag true
        flag_log = tmp / "decisions-flags.jsonl"
        flag_log.write_text(
            '\n'.join(
                [
                    json.dumps({"ts": 1, "jev_status": "winner", "dedupe": True, "stale_sidecar": True, "strong_pick": True, "over_budget": True, "prompt_head": "hello-jev", "prompt_sha": "abc123"}),
                    json.dumps({"ts": 2, "jev_status": "none"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        flag_ok = True
        for flag in ("--dedupe-only", "--stale", "--strong", "--over-budget"):
            rc, out = _run(
                [
                    str(SCRIPTS / "decisions.py"),
                    "--file",
                    str(flag_log),
                    flag,
                    "--json",
                ]
            )
            try:
                if not (rc == 0 and json.loads(out).get("total") == 1):
                    flag_ok = False
            except ValueError:
                flag_ok = False
        ok = flag_ok
    if ok:
        # --prompt substring-matches prompt_head/prompt_tail
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(flag_log),
                "--prompt",
                "HELLO",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --sha PREFIX matches prompt_sha prefix
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(flag_log),
                "--sha",
                "abc",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --week (--days 7) keeps only recent entries; old rows drop out
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--week",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 0
        except ValueError:
            ok = False
    if ok:
        # --days N bounds the window directly: far-past rows drop for small
        # N, a huge N keeps them all
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--days",
                "30",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 0
        except ValueError:
            ok = False
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "decisions.py"),
                    "--file",
                    str(log),
                    "--days",
                    "99999",
                    "--json",
                ]
            )
            try:
                ok = rc == 0 and json.loads(out).get("total") == 2
            except ValueError:
                ok = False
    if ok:
        # --since-last STATUS keeps entries after the newest row of that status
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(winner_log),
                "--since-last",
                "winner",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --where-not KEY=VAL drops matching rows
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--where-not",
                "jev_status=winner",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --max-need filters on the numeric need ceiling
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(need_log),
                "--max-need",
                "0.5",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --jq-first/--jq-last pick the first/last extracted value
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--jq",
                "jev_status",
                "--jq-first",
            ]
        )
        ok = rc == 0 and out.strip() == "winner"
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--jq",
                "jev_status",
                "--jq-last",
            ]
        )
        ok = rc == 0 and out.strip() == "none"
    if ok:
        # --jq with an unknown dotted key is graceful: null per row, rc 0
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--jq",
                "nope.nope",
            ]
        )
        ok = (
            rc == 0
            and [ln.strip() for ln in out.splitlines()] == ["null", "null"]
        )
    if ok:
        # --jq digs a flat field present on every row
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--jq",
                "harness",
            ]
        )
        ok = (
            rc == 0
            and [ln.strip() for ln in out.splitlines()] == ["smoke", "smoke"]
        )
    if ok:
        # numeric-field threshold filters keep only the high row
        num_log = tmp / "decisions-num.jsonl"
        num_log.write_text(
            '\n'.join(
                [
                    json.dumps({"ts": 1, "jev_status": "winner", "fill": "peer", "prompt_len": 100, "shortlist_n": 5, "shortlist_score_avg": 0.9, "n_catalog": 4, "latency_ms": 500}),
                    json.dumps({"ts": 2, "jev_status": "winner", "prompt_len": 1, "shortlist_n": 1, "shortlist_score_avg": 0.1, "n_catalog": 0, "latency_ms": 9000}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        num_ok = True
        for flag, val in (
            ("--fill", "peer"),
            ("--min-prompt-len", "50"),
            ("--min-shortlist", "3"),
            ("--min-score", "0.5"),
            ("--min-catalog", "2"),
            ("--max-latency", "1000"),
            ("--min-latency", "1000"),
        ):
            rc, out = _run(
                [
                    str(SCRIPTS / "decisions.py"),
                    "--file",
                    str(num_log),
                    flag,
                    val,
                    "--json",
                ]
            )
            try:
                if not (rc == 0 and json.loads(out).get("total") == 1):
                    num_ok = False
            except ValueError:
                num_ok = False
        ok = num_ok
    if ok:
        # --sample N emits N matching entries as JSON lines
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--sample",
                "1",
            ]
        )
        ok = rc == 0 and len([ln for ln in out.splitlines() if ln.strip()]) >= 1
    if ok:
        # --csv emits a header plus one row per entry, no stats
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--csv",
            ]
        )
        lines = out.strip().splitlines()
        ok = (
            rc == 0
            and len(lines) == 3
            and lines[0].startswith("ts,")
            and lines[2].split(",")[2] == "none"
        )
    if ok:
        # --md emits a Markdown table (header, separator, rows)
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--md",
            ]
        )
        lines = out.strip().splitlines()
        ok = (
            rc == 0
            and len(lines) == 4
            and lines[0].startswith("| ts")
            and "| none |" in lines[3]
        )
    if ok:
        # --skip 1 drops the first match; --jsonl emits the rest raw
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--skip",
                "1",
                "--jsonl",
            ]
        )
        lines = out.strip().splitlines()
        try:
            ok = (
                rc == 0
                and len(lines) == 1
                and json.loads(lines[0]).get("jev_status") == "none"
            )
        except ValueError:
            ok = False
    if ok:
        # --out writes the filtered entries as JSONL instead of printing
        filtered = tmp / "filtered.jsonl"
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--out",
                str(filtered),
            ]
        )
        try:
            rows = [
                json.loads(line)
                for line in filtered.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            ok = rc == 0 and len(rows) == 2
        except (OSError, ValueError):
            ok = False
    if ok:
        # --report writes a markdown stats report to PATH
        report = tmp / "report.md"
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--report",
                str(report),
            ]
        )
        ok = rc == 0 and report.is_file() and "winner" in report.read_text(encoding="utf-8")
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--status",
                "none",
                "--prune",
            ]
        )
        try:
            lines = log.read_text(encoding="utf-8").splitlines()
            ok = (
                rc == 0
                and len(lines) == 1
                and json.loads(lines[0]).get("jev_status") == "none"
            )
        except (ValueError, IndexError):
            ok = False
    if ok:
        # --watch emits {ts,count} ticks until --max-ticks stops it
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--watch",
                "0.05",
                "--max-ticks",
                "2",
            ]
        )
        ok = rc == 0 and '"count": 1' in out and "watch tick=2" in out
    if ok:
        # --quiet keeps passing ticks off stdout (stderr still logs them)
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--quiet",
            ]
        )
        stdout_ticks = [ln for ln in out.splitlines() if '"count"' in ln]
        ok = rc == 0 and not stdout_ticks and "watch tick=2" in out
    if ok:
        # --watch-max S bounds the loop by elapsed seconds
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--watch",
                "0.02",
                "--watch-max",
                "0.05",
            ]
        )
        ok = rc == 0 and "watch tick=" in out
    if ok:
        # --fail-fast breaks on the first tick with removals: shrink the
        # log mid-watch and the run exits 1 with verdict "removed"
        shrink_log = tmp / "decisions-shrink.jsonl"
        shrink_log.write_text(
            '\n'.join(
                json.dumps({"ts": t, "jev_status": "winner"})
                for t in (1, 2)
            )
            + "\n",
            encoding="utf-8",
        )
        wv = tmp / "decisions-watch-verdict.json"
        wout = ""
        proc = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(shrink_log),
                "--watch",
                "0.15",
                "--fail-fast",
                "--verdict",
                str(wv),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            time.sleep(0.4)  # let the first tick sample the full log
            shrink_log.write_text(
                json.dumps({"ts": 1, "jev_status": "winner"}) + "\n",
                encoding="utf-8",
            )
            wout, _ = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
        ok = proc.returncode == 1 and '"removed": 1' in wout
        try:
            ok = ok and json.loads(wv.read_text(encoding="utf-8")).get(
                "verdict"
            ) == "removed"
        except (OSError, ValueError):
            ok = False
    if ok:
        # --verdict on an empty log writes {verdict: empty}
        empty_log = tmp / "decisions-empty.jsonl"
        empty_log.write_text("", encoding="utf-8")
        verdict = tmp / "decisions-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(empty_log),
                "--verdict",
                str(verdict),
            ]
        )
        try:
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            ok = rc == 0 and payload.get("verdict") == "empty"
        except (OSError, ValueError):
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
    if ok:
        # notes --prune N rewrites the trace keeping only the last N notes
        _run(
            [
                str(SCRIPTS / "trace.py"),
                "--file",
                str(trace_file),
                "record",
                "--pick",
                "smoke-pick-2",
                "--note",
                "second note",
            ]
        )
        rc, out = _run(
            [
                str(SCRIPTS / "trace.py"),
                "--file",
                str(trace_file),
                "notes",
                "--prune",
                "1",
            ]
        )
        if rc == 0:
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "notes",
                    "--json",
                ]
            )
            try:
                notes = json.loads(out)
                ok = rc == 0 and len(notes) == 1 and "second" in str(notes[0].get("text", ""))
            except (ValueError, IndexError):
                ok = False
        else:
            ok = False
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "trace.py"),
                "--file",
                str(trace_file),
                "history",
                "--json",
            ]
        )
        if rc == 0:
            try:
                ok = any(
                    "smoke-pick" in str(p.get("pick", ""))
                    for p in json.loads(out)
                )
            except (ValueError, AttributeError):
                ok = False
        else:
            ok = False
    if ok:
        # export dumps the whole trace bundle (state + history + notes)
        rc, out = _run(
            [
                str(SCRIPTS / "trace.py"),
                "--file",
                str(trace_file),
                "export",
            ]
        )
        if rc == 0:
            try:
                bundle = json.loads(out)
                ok = (
                    "notes" in bundle
                    and "file" in bundle
                    and bundle.get("plan") == "smoke"
                )
            except ValueError:
                ok = False
        else:
            ok = False
    if ok:
        # bump/set/show/stats mutate and report the same trace
        rc, out = _run(
            [str(SCRIPTS / "trace.py"), "--file", str(trace_file), "bump"]
        )
        ok = rc == 0
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "set",
                    "--step",
                    "smoke-step",
                    "--kv",
                    "mood=green",
                ]
            )
            ok = rc == 0
        if ok:
            rc, out = _run(
                [str(SCRIPTS / "trace.py"), "--file", str(trace_file), "show"]
            )
            try:
                # show filters to known keys; --kv lands on the raw file
                trace = json.loads(out).get("trace", {})
                raw = json.loads(trace_file.read_text(encoding="utf-8"))
                ok = (
                    rc == 0
                    and trace.get("current_step") == "smoke-step"
                    and trace.get("attempt_count") == 1
                    and raw.get("mood") == "green"
                )
            except (ValueError, AttributeError, OSError):
                ok = False
        if ok:
            rc, out = _run(
                [str(SCRIPTS / "trace.py"), "--file", str(trace_file), "stats"]
            )
            try:
                stats = json.loads(out)
                ok = (
                    rc == 0
                    and stats.get("attempt_count") == 1
                    and stats.get("history") == 2
                    and stats.get("exists") is True
                )
            except (ValueError, AttributeError):
                ok = False
        if ok:
            # notes --limit 1 tails the note list
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "notes",
                    "--limit",
                    "1",
                ]
            )
            ok = rc == 0 and "second note" in out
        if ok:
            # history --watch emits {picks} ticks; --verdict writes the probe
            verdict = tmp / "trace-verdict.json"
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "history",
                    "--watch",
                    "0.03",
                    "--max-ticks",
                    "2",
                    "--verdict",
                    str(verdict),
                ]
            )
            ticks = [ln for ln in out.splitlines() if '"picks"' in ln]
            ok = rc == 0 and len(ticks) == 2
            try:
                ok = ok and json.loads(verdict.read_text(encoding="utf-8")).get(
                    "verdict"
                ) == "picks"
            except (OSError, ValueError):
                ok = False
        if ok:
            # stats --watch emits {exists,attempt_count} ticks + verdict probe
            verdict = tmp / "trace-stats-verdict.json"
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "stats",
                    "--watch",
                    "0.03",
                    "--max-ticks",
                    "2",
                    "--verdict",
                    str(verdict),
                ]
            )
            ticks = [ln for ln in out.splitlines() if '"exists"' in ln]
            ok = rc == 0 and len(ticks) == 2
            try:
                ok = ok and json.loads(verdict.read_text(encoding="utf-8")).get(
                    "verdict"
                ) == "exists"
            except (OSError, ValueError):
                ok = False
        if ok:
            # notes --watch emits {notes} ticks + verdict probe
            verdict = tmp / "trace-notes-verdict.json"
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "notes",
                    "--watch",
                    "0.03",
                    "--max-ticks",
                    "2",
                    "--verdict",
                    str(verdict),
                ]
            )
            ticks = [ln for ln in out.splitlines() if '"notes"' in ln]
            ok = rc == 0 and len(ticks) == 2
            try:
                ok = ok and json.loads(verdict.read_text(encoding="utf-8")).get(
                    "verdict"
                ) == "notes"
            except (OSError, ValueError):
                ok = False
    if ok:
        # prune removes a trace file whose mtime is older than the TTL
        os.utime(trace_file, (time.time() - 4000,) * 2)
        rc, out = _run(
            [
                str(SCRIPTS / "trace.py"),
                "--file",
                str(trace_file),
                "prune",
                "--older-than",
                "60",
            ]
        )
        try:
            payload = json.loads(out)
            ok = (
                rc == 0
                and payload.get("removed") is True
                and not trace_file.exists()
            )
        except ValueError:
            ok = False
    return _step("trace", ok, "rc=%d" % rc if ok else out.strip()[:160])


def step_skill_lint(tmp: Path) -> dict:
    skill = SKILL_DIR / "SKILL.md"
    rc, out = _run([str(SCRIPTS / "skill_lint.py"), str(skill)])
    ok = rc == 0
    if ok:
        # --json emits a findings payload; the shipped SKILL.md is clean
        rc, out = _run([str(SCRIPTS / "skill_lint.py"), str(skill), "--json"])
        try:
            ok = rc == 0 and json.loads(out).get("findings") == []
        except ValueError:
            ok = False
    if ok:
        # --explain RULE prints one rule's description
        rc, out = _run(
            [str(SCRIPTS / "skill_lint.py"), str(skill), "--explain", "S001"]
        )
        ok = rc == 0 and "S001" in out
    if ok:
        # --strict/--severity floors stay green on the shipped skill
        rc, out = _run(
            [
                str(SCRIPTS / "skill_lint.py"),
                str(skill),
                "--strict",
                "--severity",
                "info",
            ]
        )
        ok = rc == 0
    if ok:
        # --watch emits {findings,errors} ticks; --verdict writes the probe;
        # --out appends the payload file
        verdict = tmp / "slint-verdict.json"
        out_file = tmp / "slint-out.log"
        rc, out = _run(
            [
                str(SCRIPTS / "skill_lint.py"),
                str(skill),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--verdict",
                str(verdict),
                "--out",
                str(out_file),
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = rc == 0 and len(ticks) == 2 and out_file.is_file()
        try:
            ok = ok and json.loads(verdict.read_text(encoding="utf-8")).get(
                "verdict"
            ) == "pass"
        except (OSError, ValueError):
            ok = False
    if ok:
        # --quiet keeps passing ticks off stdout (stderr still logs them)
        rc, out = _run(
            [
                str(SCRIPTS / "skill_lint.py"),
                str(skill),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--quiet",
            ]
        )
        stdout_ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = rc == 0 and not stdout_ticks and "watch tick=2" in out
    if ok:
        # --fail-fast stops the watch on the first erroring tick
        bad_dir = tmp / "bad-skill"
        bad_dir.mkdir(exist_ok=True)
        (bad_dir / "SKILL.md").write_text(
            "no frontmatter here\n", encoding="utf-8"
        )
        rc, out = _run(
            [
                str(SCRIPTS / "skill_lint.py"),
                str(bad_dir / "SKILL.md"),
                "--watch",
                "0.03",
                "--max-ticks",
                "5",
                "--fail-fast",
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = len(ticks) == 1 and "watch tick=2" not in out
    return _step("skill_lint", ok, out.strip()[:160] or "rc=%d" % rc)


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
    if ok:
        # --json emits a findings array; the good request is clean
        rc, out = _run(
            [str(SCRIPTS / "question_lint.py"), str(req), "--json"]
        )
        try:
            ok = rc == 0 and json.loads(out).get("findings") == []
        except (ValueError, AttributeError):
            ok = False
    if ok:
        # --explain RULE prints one rule's description
        rc, out = _run(
            [str(SCRIPTS / "question_lint.py"), "--explain", "J001"]
        )
        ok = rc == 0 and "J001" in out
    if ok:
        # --watch emits {findings,errors} ticks; --verdict writes the probe
        verdict = tmp / "qlint-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "question_lint.py"),
                str(req),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--verdict",
                str(verdict),
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = rc == 0 and len(ticks) == 2
        try:
            ok = ok and json.loads(verdict.read_text(encoding="utf-8")).get(
                "verdict"
            ) == "pass"
        except (OSError, ValueError):
            ok = False
    if ok:
        # --quiet keeps passing ticks off stdout (stderr still logs them)
        rc, out = _run(
            [
                str(SCRIPTS / "question_lint.py"),
                str(req),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--quiet",
            ]
        )
        stdout_ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = rc == 0 and not stdout_ticks and "watch tick=2" in out
    if ok:
        # --fail-fast stops the watch on the first erroring tick
        bad_req = tmp / "req-bad.json"
        bad_req.write_text(
            json.dumps(
                {
                    "questions": {
                        "q": {
                            "type": "noul",
                            "instructions": "Go?",
                            "criteria": {"true": "x", "false": "x"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "question_lint.py"),
                str(bad_req),
                "--watch",
                "0.03",
                "--max-ticks",
                "5",
                "--fail-fast",
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = len(ticks) == 1 and "watch tick=2" not in out
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
    if ok:
        # --json emits {"outcome": ...} objects instead of bare tokens
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
                "--json",
            ],
            env=env,
        )
        if rc == 0:
            try:
                ok = json.loads(out.strip().splitlines()[0]).get("outcome") == "human"
            except (ValueError, IndexError, AttributeError):
                ok = False
        else:
            ok = False
    if ok:
        # --status --jq digs one dotted field; a bad key exits 2
        rc, out = _run(
            [
                str(SCRIPTS / "apply_fill.py"),
                "--status",
                "--cwd",
                str(tmp / "cwd"),
                "--jq",
                "miss",
            ],
            env=env,
        )
        ok = rc == 0 and out.strip() in ("true", "false")
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "apply_fill.py"),
                    "--status",
                    "--cwd",
                    str(tmp / "cwd"),
                    "--jq",
                    "nope.nope",
                ],
                env=env,
            )
            ok = rc == 2
    if ok:
        # --watch re-scans for miss/ask markers each tick; --verdict
        # writes the slim {verdict: clean|pending} probe
        verdict = tmp / "apply-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "apply_fill.py"),
                "--cwd",
                str(tmp / "cwd"),
                "--watch",
                "0.05",
                "--max-ticks",
                "2",
                "--verdict",
                str(verdict),
            ],
            env=env,
        )
        ticks = [ln for ln in out.splitlines() if '"miss"' in ln]
        ok = rc in (0, 1) and len(ticks) == 2
        try:
            ok = ok and "verdict" in json.loads(
                verdict.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            ok = False
    if ok:
        # --quiet keeps clean ticks off stdout (stderr still logs them)
        clean_cwd = tmp / "apply-clean-cwd"
        clean_cwd.mkdir(parents=True, exist_ok=True)
        rc, out = _run(
            [
                str(SCRIPTS / "apply_fill.py"),
                "--cwd",
                str(clean_cwd),
                "--watch",
                "0.05",
                "--max-ticks",
                "2",
                "--quiet",
            ],
            env=env,
        )
        stdout_ticks = [ln for ln in out.splitlines() if '"miss"' in ln]
        ok = rc in (0, 1) and not stdout_ticks and "watch tick=2" in out
    if ok:
        # --fail-fast stops the watch on the first tick with a pending miss
        miss_cwd = tmp / "apply-miss-cwd"
        miss_cwd.mkdir(parents=True, exist_ok=True)
        (miss_cwd / ".jev-tools-miss.json").write_text(
            json.dumps({"task": "smoke", "written_at": time.time()}),
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "apply_fill.py"),
                "--cwd",
                str(miss_cwd),
                "--watch",
                "0.05",
                "--max-ticks",
                "5",
                "--fail-fast",
            ],
            env=env,
        )
        ticks = [ln for ln in out.splitlines() if '"miss"' in ln]
        ok = len(ticks) == 1 and "watch tick=2" not in out
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
        miss = cwd / ".jev-tools-miss.json"
        ok = miss.is_file()
        if ok:
            try:
                marker = json.loads(miss.read_text(encoding="utf-8"))
                ok = (
                    marker.get("task") == "smoke test task"
                    and isinstance(marker.get("written_at"), int)
                    and marker.get("empty") is True
                )
            except ValueError:
                ok = False
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
    if ok:
        # emit shapes: claude-code -> hookSpecificOutput, grok -> {}
        for harness, want in (("claude-code", '"hookSpecificOutput"'), ("grok", None)):
            env["JEV_HOOK_HARNESS"] = harness
            rc, out = _run(
                [str(SCRIPTS / "inventory_hook.py")],
                cwd=tmp,
                env=env,
                inp=json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "emit shape check %s" % harness,
                        "cwd": str(cwd),
                    }
                ),
            )
            if rc != 0:
                ok = False
                break
            first = out.strip().splitlines()[0] if out.strip() else ""
            if want is None:
                ok = first == "{}"
            else:
                ok = want in first
            if not ok:
                break
    if ok:
        # --events --json emits the allowed event names as an array
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py"), "--events", "--json"],
            cwd=tmp,
            env=env,
        )
        if rc == 0:
            try:
                ok = "UserPromptSubmit" in json.loads(out.strip().splitlines()[0])
            except (ValueError, TypeError, IndexError):
                ok = False
        else:
            ok = False
    if ok:
        # --env reports the resolved config (presence flags, never secrets)
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py"), "--env"], cwd=tmp, env=env
        )
        if rc == 0:
            try:
                report = json.loads(out.strip())
                ok = "events" in report and "limit" in report
            except (ValueError, AttributeError):
                ok = False
        else:
            ok = False
    if ok:
        # JEV_HOOK_MAX_PROMPT truncates the prompt; --json reports it
        env.pop("JEV_HOOK_HARNESS", None)
        env["JEV_HOOK_MAX_PROMPT"] = "10"
        cwd2 = tmp / "cwd2"
        cwd2.mkdir(exist_ok=True)
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py"), "--json"],
            cwd=tmp,
            env=env,
            inp=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "a prompt much longer than ten characters",
                    "cwd": str(cwd2),
                }
            ),
        )
        env.pop("JEV_HOOK_MAX_PROMPT", None)
        ok = rc == 0 and '"prompt_truncated": true' in out
    if ok:
        # JEV_HOOK_OFF emits {} and --verbose says why
        env["JEV_HOOK_OFF"] = "1"
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py"), "--verbose"],
            cwd=tmp,
            env=env,
            inp=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "kill switch smoke",
                    "cwd": str(cwd2),
                }
            ),
        )
        env.pop("JEV_HOOK_OFF", None)
        ok = rc == 0 and "{}" in out and "disabled" in out
    if ok:
        # JEV_HOOK_SKIP_EVENTS removes the event from the allowed set
        env["JEV_HOOK_SKIP_EVENTS"] = "UserPromptSubmit"
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py"), "--verbose"],
            cwd=tmp,
            env=env,
            inp=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "skipped event smoke",
                    "cwd": str(cwd2),
                }
            ),
        )
        env.pop("JEV_HOOK_SKIP_EVENTS", None)
        ok = rc == 0 and "{}" in out
    if ok:
        # JEV_HOOK_EVENTS replaces the allowed set outright
        env["JEV_HOOK_EVENTS"] = "PreToolUse"
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py")],
            cwd=tmp,
            env=env,
            inp=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "restricted event smoke",
                    "cwd": str(cwd2),
                }
            ),
        )
        env.pop("JEV_HOOK_EVENTS", None)
        ok = rc == 0 and "{}" in out
    if ok:
        # a payload timestamp older than JEV_HOOK_MAX_AGE is dropped
        env["JEV_HOOK_MAX_AGE"] = "60"
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py"), "--verbose"],
            cwd=tmp,
            env=env,
            inp=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "stale prompt smoke",
                    "cwd": str(cwd2),
                    "timestamp": 1000000,
                }
            ),
        )
        env.pop("JEV_HOOK_MAX_AGE", None)
        ok = rc == 0 and "{}" in out
    if ok:
        # an empty prompt emits {} (nothing to consult on)
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py")],
            cwd=tmp,
            env=env,
            inp=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "",
                    "cwd": str(cwd2),
                }
            ),
        )
        ok = rc == 0 and out.strip().splitlines()[0] == "{}"
    if ok:
        # $name in the prompt is an explicit pick — no Jev call needed
        skill_dir = tmp / "hermes" / "skills" / "explicit-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: explicit-skill\n---\n", encoding="utf-8"
        )
        env["HERMES_HOME"] = str(tmp / "hermes")
        cwd3 = tmp / "cwd3"
        cwd3.mkdir(exist_ok=True)
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py"), "--json"],
            cwd=tmp,
            env=env,
            inp=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "please apply $explicit-skill to this task",
                    "cwd": str(cwd3),
                }
            ),
        )
        env.pop("HERMES_HOME", None)
        ok = (
            rc == 0
            and '"explicit": true' in out
            and '"jev_status": "winner"' in out
        )
    if ok:
        # JEV_HOOK_WINNER forces the pick by name (question=env)
        env["HERMES_HOME"] = str(tmp / "hermes")
        env["JEV_HOOK_WINNER"] = "explicit-skill"
        cwd4 = tmp / "cwd4"
        cwd4.mkdir(exist_ok=True)
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py"), "--json"],
            cwd=tmp,
            env=env,
            inp=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "an unrelated prompt",
                    "cwd": str(cwd4),
                }
            ),
        )
        env.pop("JEV_HOOK_WINNER", None)
        env.pop("HERMES_HOME", None)
        ok = (
            rc == 0
            and '"jev_status": "winner"' in out
            and '"question": "env"' in out
        )
    if ok:
        # fail-open: with a key set but Jev unreachable the hook still emits {}
        bad_policy = tmp / "bad-policy.json"
        try:
            pdata = json.loads((SKILL_DIR / "policy.json").read_text(encoding="utf-8"))
            pdata["endpoint"] = "http://127.0.0.1:1/v1/systemone"
            bad_policy.write_text(json.dumps(pdata), encoding="utf-8")
        except (OSError, ValueError):
            ok = False
        if ok:
            env5 = dict(env)
            env5["JEV_POLICY"] = str(bad_policy)
            env5["TYPESAFE_API_KEY"] = "smoke-dummy"
            env5["JEV_HOOK_RETRIES"] = "0"
            rc, out = _run(
                [str(SCRIPTS / "inventory_hook.py")], cwd=tmp, env=env5, inp=payload
            )
            try:
                ok = rc == 0 and isinstance(
                    json.loads(out.strip().splitlines()[0]), dict
                )
            except (ValueError, IndexError):
                ok = False
    if ok:
        # --debug echoes LAST_DECISION to stderr; --out persists the payload;
        # --verdict writes the slim {verdict,...} probe
        out_path = tmp / "hook-emit.json"
        verdict_path = tmp / "hook-verdict.json"
        cwd5 = tmp / "cwd5"
        cwd5.mkdir(exist_ok=True)
        rc, out = _run(
            [
                str(SCRIPTS / "inventory_hook.py"),
                "--debug",
                "--out",
                str(out_path),
                "--verdict",
                str(verdict_path),
            ],
            cwd=tmp,
            env=env,
            inp=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "debug out verdict smoke",
                    "cwd": str(cwd5),
                }
            ),
        )
        ok = (
            rc == 0
            and "jev_status=" in out
            and out_path.is_file()
            and verdict_path.is_file()
        )
        try:
            emitted = json.loads(out_path.read_text(encoding="utf-8"))
            verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
            ok = ok and isinstance(emitted, dict) and "verdict" in verdict
        except (OSError, ValueError):
            ok = False
    if ok:
        # --watch S --max-ticks N emits N tick lines and stops
        event_file = tmp / "hook-event.json"
        event_file.write_text(payload, encoding="utf-8")
        rc, out = _run(
            [
                str(SCRIPTS / "inventory_hook.py"),
                "--file",
                str(event_file),
                "--watch",
                "0.05",
                "--max-ticks",
                "2",
            ],
            cwd=tmp,
            env=env,
        )
        # exits 1 when no tick produced a winner
        ticks = [ln for ln in out.splitlines() if '"winner_changed"' in ln]
        ok = rc in (0, 1) and len(ticks) == 2 and 'watch tick=2' in out
    return _step("hook", ok, out.strip()[:120] or "rc=%d" % rc)


def step_install(tmp: Path) -> dict:
    repo_root = SCRIPTS.parent.parent.parent
    env = dict(os.environ)  # skillscan:allow
    home = tmp / "install-home"
    env["USERPROFILE"] = str(home)
    env["HOME"] = str(home)
    rc, out = _run(
        [
            str(repo_root / "scripts" / "install.py"),
            "--dry-run",
            "--agents",
            "codex",
        ],
        env=env,
    )
    # dry-run plans into the redirected home and writes nothing there
    writes = [p for p in home.rglob("*") if p.is_file()]
    ok = rc == 0 and "skill ->" in out and not writes
    if ok:
        # --check-key reports set/missing and never echoes a value
        env.pop("TYPESAFE_API_KEY", None)
        rc, out = _run(
            [str(repo_root / "scripts" / "install.py"), "--check-key"],
            env=env,
        )
        ok = (
            rc == 0
            and out.strip().startswith("TYPESAFE_API_KEY:")
            and "apikey_" not in out
        )
    return _step("install", ok, out.strip().splitlines()[-1][:120] if out.strip() else "rc=%d" % rc)


def step_compare(tmp: Path) -> dict:
    rc, out = _run([str(SCRIPTS / "compare.py"), "--strict"])
    ok = rc == 0 and "after_jev" in out
    if ok:
        # --json emits the machine-readable payload with per-case rows
        rc, out = _run([str(SCRIPTS / "compare.py"), "--json"])
        try:
            payload = json.loads(out)
            ok = (
                rc == 0
                and isinstance(payload, dict)
                and payload.get("rows")
                and all("after" in c and "before" in c for c in payload["rows"])
            )
        except (ValueError, AttributeError, TypeError):
            ok = False
    if ok:
        # --only runs a single case id
        case_id = payload["rows"][0].get("id", "")
        rc, out = _run(
            [str(SCRIPTS / "compare.py"), "--json", "--only", case_id]
        )
        try:
            ok = rc == 0 and len(json.loads(out).get("rows", [])) == 1
        except ValueError:
            ok = False
    if ok:
        # --md prints a markdown table, --verdict writes the slim probe
        verdict = tmp / "compare-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "compare.py"),
                "--md",
                "--verdict",
                str(verdict),
            ]
        )
        ok = rc == 0 and "|" in out
        try:
            ok = ok and "verdict" in json.loads(verdict.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ok = False
    if ok:
        # a bad --jq key exits 2
        rc, out = _run([str(SCRIPTS / "compare.py"), "--json", "--jq", "nope"])
        ok = rc == 2
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
    ok = ok_json and rc in (0, 1)
    if ok:
        # --agents filters the checks to just the named harnesses
        rc, out = _run(
            [
                str(SCRIPTS / "doctor.py"),
                "--agents",
                "codex",
                "--home",
                str(tmp / "home"),
                "--hermes-home",
                str(tmp / "hermes"),
            ]
        )
        try:
            payload = json.loads(out)
            agents_seen = {
                c.get("agent")
                for c in payload.get("checks", [])
                if c.get("agent") and c.get("agent") != "*"
            }
            ok = rc in (0, 1) and agents_seen == {"codex"}
        except ValueError:
            ok = False
    return _step("doctor_json", ok, "rc=%d" % rc)


def step_trigger_lint(tmp: Path) -> dict:
    cases = SCRIPTS.parent.parent.parent / "tests" / "fixtures" / "jev-consult.trigger-cases.json"
    if not cases.is_file():
        return _step("trigger_lint", False, "fixture missing: %s" % cases)
    rc, out = _run([str(SCRIPTS / "trigger_lint.py"), str(cases)])
    ok = rc == 0
    if ok:
        # --json emits a findings payload; the shipped fixture is clean
        rc, out = _run(
            [str(SCRIPTS / "trigger_lint.py"), str(cases), "--json"]
        )
        try:
            ok = rc == 0 and json.loads(out).get("findings") == []
        except (ValueError, AttributeError):
            ok = False
    if ok:
        # --explain RULE prints one rule's description
        rc, out = _run(
            [str(SCRIPTS / "trigger_lint.py"), "--explain", "T001"]
        )
        ok = rc == 0 and "T001" in out
    if ok:
        # --watch emits {findings,errors} ticks; --verdict writes the probe
        verdict = tmp / "tlint-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "trigger_lint.py"),
                str(cases),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--verdict",
                str(verdict),
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = rc == 0 and len(ticks) == 2
        try:
            ok = ok and json.loads(verdict.read_text(encoding="utf-8")).get(
                "verdict"
            ) == "pass"
        except (OSError, ValueError):
            ok = False
    if ok:
        # --quiet keeps passing ticks off stdout (stderr still logs them)
        rc, out = _run(
            [
                str(SCRIPTS / "trigger_lint.py"),
                str(cases),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--quiet",
            ]
        )
        stdout_ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = rc == 0 and not stdout_ticks and "watch tick=2" in out
    if ok:
        # --fail-fast stops the watch on the first erroring tick
        bad_cases = tmp / "cases-bad.json"
        bad_cases.write_text(
            json.dumps({"skill": "x", "cases": [{"id": "c1"}]}),
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "trigger_lint.py"),
                str(bad_cases),
                "--watch",
                "0.03",
                "--max-ticks",
                "5",
                "--fail-fast",
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = len(ticks) == 1 and "watch tick=2" not in out
    return _step("trigger_lint", ok, out.strip()[:120] or "rc=%d" % rc)


def step_trigger_eval(tmp: Path) -> dict:
    rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--quiet"])
    ok = rc == 0 and "PASS" in out
    if ok:
        # --json emits the scored-case payload
        rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--json"])
        try:
            payload = json.loads(out)
            ok = (
                rc == 0
                and payload.get("ok") is True
                and isinstance(payload.get("cases"), list)
                and payload["cases"]
            )
        except ValueError:
            ok = False
    if ok:
        # --coverage reports the positive-prompt hit ratio
        rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--coverage"])
        ok = rc == 0 and "coverage:" in out
    if ok:
        # --score rates an ad-hoc prompt against the description
        rc, out = _run(
            [str(SCRIPTS / "trigger_eval.py"), "--score", "refactor this function"]
        )
        ok = rc == 0 and "score=" in out
    if ok:
        # a bad --jq key exits 2
        rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--json", "--jq", "nope"])
        ok = rc == 2
    if ok:
        # --watch emits {verdict,ok,coverage} ticks; --verdict writes the
        # slim eval probe
        verdict = tmp / "teval-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "trigger_eval.py"),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--verdict",
                str(verdict),
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"verdict"' in ln]
        ok = rc == 0 and len(ticks) == 2
        try:
            ok = ok and json.loads(verdict.read_text(encoding="utf-8")).get(
                "verdict"
            ) == "PASS"
        except (OSError, ValueError):
            ok = False
    if ok:
        # row-shaping flags: --ids lists case ids, --id filters to one,
        # --csv emits comma rows, --summary collapses to the stats line
        rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--ids"])
        ids = [ln.strip() for ln in out.splitlines() if ln.strip()]
        ok = rc == 0 and len(ids) >= 2
        if ok:
            rc, out = _run(
                [str(SCRIPTS / "trigger_eval.py"), "--id", ids[0], "--quiet"]
            )
            ok = rc == 0 and "PASS" in out
        if ok:
            rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--csv"])
            ok = rc == 0 and any(
                ln.split(",")[0] == ids[0] for ln in out.splitlines()
            )
        if ok:
            rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--summary"])
            ok = rc == 0 and "PASS" in out
        if ok:
            # --top N keeps only the N weakest rows
            rc, out = _run(
                [str(SCRIPTS / "trigger_eval.py"), "--top", "2", "--ids"]
            )
            ok = rc == 0 and len(
                [ln for ln in out.splitlines() if ln.strip()]
            ) == 2
        if ok:
            # --min-coverage gates the hit rate: 0 passes, >1 fails
            rc, _ = _run(
                [str(SCRIPTS / "trigger_eval.py"), "--min-coverage", "0"]
            )
            ok = rc == 0
            rc, _ = _run(
                [str(SCRIPTS / "trigger_eval.py"), "--min-coverage", "1.1"]
            )
            ok = ok and rc == 1
        if ok:
            rc, _ = _run([str(SCRIPTS / "trigger_eval.py"), "--uncovered"])
            ok = rc == 0
    if ok:
        # coverage/reporting flags: --covers counts per tag, --covers-map
        # lists tag->ids, --dist prints the score histogram, --tokens shows
        # matched tokens per row, --prompts lists id: prompt lines
        rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--covers"])
        ok = rc == 0 and len(out.strip().splitlines()) >= 1
        if ok:
            rc, out = _run(
                [str(SCRIPTS / "trigger_eval.py"), "--covers-map"]
            )
            ok = rc == 0 and len(out.strip().splitlines()) >= 1
        if ok:
            rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--dist"])
            ok = rc == 0 and "-" in out
        if ok:
            rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--tokens"])
            ok = rc == 0 and "tokens=" in out
        if ok:
            rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--prompts"])
            ok = rc == 0 and ":" in out
    if ok:
        # gate/sort flags: --min-covers 1 passes (every tag has a case),
        # --min-covers 3 fails; --sort orders weakest-first; --desc swaps
        # the description the prompts are scored against
        rc, _ = _run([str(SCRIPTS / "trigger_eval.py"), "--min-covers", "1"])
        ok = rc == 0
        rc, _ = _run([str(SCRIPTS / "trigger_eval.py"), "--min-covers", "3"])
        ok = ok and rc == 1
        if ok:
            rc, out = _run(
                [str(SCRIPTS / "trigger_eval.py"), "--sort", "--ids"]
            )
            ok = rc == 0 and len(
                [ln for ln in out.splitlines() if ln.strip()]
            ) >= 2
        if ok:
            rc, out = _run(
                [str(SCRIPTS / "trigger_eval.py"), "--desc", "refactor code"]
            )
            ok = rc in (0, 1) and "margin:" in out
    if ok:
        # --fail prints only failing rows (none on the shipped fixture);
        # --min-score F keeps only rows scoring >= F
        rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--fail"])
        ok = rc == 0 and "PASS" in out
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trigger_eval.py"),
                    "--min-score",
                    "1.0",
                    "--ids",
                ]
            )
            ok = rc == 0 and all(
                ln.strip().startswith("pos-")
                for ln in out.splitlines()
                if ln.strip()
            )
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
        # lossless spill: the full 90k result lands on disk while the
        # emitted tool output is abridged
        try:
            files = list(spill.iterdir())
            ok = files and files[0].stat().st_size >= 90000
        except OSError:
            ok = False
    if ok:
        try:
            emitted = json.loads(out.strip().splitlines()[0])
            abridged = emitted.get("hookSpecificOutput", {}).get(
                "updatedToolOutput", ""
            )
            ok = 0 < len(abridged) < 90000
        except (ValueError, AttributeError):
            ok = False
    if ok:
        # compact --list-spill reports the spilled payloads (path/size/mtime)
        rc, out = _run(
            [
                str(SCRIPTS / "compact.py"),
                "--list-spill",
                "--spill-dir",
                str(spill),
                "--json",
            ]
        )
        try:
            payload = json.loads(out)
            ok = (
                rc == 0
                and payload.get("count") == len(files)
                and all(f.get("size", 0) > 0 for f in payload.get("files", []))
            )
        except ValueError:
            ok = False
    if ok:
        # --verify-spill resolves the emitted "full output saved: P …]"
        # marker against the spill dir; --orphan-spill lists files no
        # emitted doc references
        doc = tmp / "emitted.txt"
        doc.write_text(
            "head\n[… 9000 chars omitted; full output saved: %s …]\ntail\n"
            % files[0],
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "compact.py"),
                "--verify-spill",
                str(doc),
                "--spill-dir",
                str(spill),
            ]
        )
        ok = rc == 0 and "0 missing" in out
        if ok:
            orphan = spill / "orphan.txt"
            orphan.write_text("unreferenced", encoding="utf-8")
            rc, out = _run(
                [
                    str(SCRIPTS / "compact.py"),
                    "--orphan-spill",
                    str(doc),
                    "--spill-dir",
                    str(spill),
                ]
            )
            ok = rc == 0 and "orphan.txt" in out
            try:
                orphan.unlink()
            except OSError:
                pass
    if ok:
        # --prune-spill unlinks spill files older than the TTL
        stale = spill / "stale.txt"
        stale.write_text("old payload", encoding="utf-8")
        os.utime(stale, (time.time() - 4000,) * 2)
        rc, out = _run(
            [
                str(SCRIPTS / "compact.py"),
                "--prune-spill",
                "60",
                "--spill-dir",
                str(spill),
            ]
        )
        ok = rc == 0 and not stale.is_file()
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
    if ok:
        # tight top-two gap on an irreversible question escalates
        tight = tmp / "answers-tight.json"
        tight.write_text(
            json.dumps(
                {
                    "irreversible": True,
                    "answers": {
                        "where": {
                            "type": "choice",
                            "choice": "refactor",
                            "confidence": 0.9,
                            "probabilities": {
                                "refactor": 0.53,
                                "rewrite": 0.47,
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "jev.py"),
                "decide",
                str(tight),
                "--jq",
                "decision.action",
            ]
        )
        ok = rc == 2 and out.strip() == '"escalate"'
    if ok:
        # --irreversible flag escalates a tight gap even without the
        # payload field; --out persists the decision payload; a bad
        # --jq key exits 2
        tight_flag = tmp / "answers-tight-flag.json"
        tight_flag.write_text(
            json.dumps(
                {
                    "answers": {
                        "where": {
                            "type": "choice",
                            "choice": "refactor",
                            "confidence": 0.9,
                            "probabilities": {
                                "refactor": 0.53,
                                "rewrite": 0.47,
                            },
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
                str(tight_flag),
                "--irreversible",
                "--jq",
                "decision.action",
            ]
        )
        ok = rc == 2 and out.strip() == '"escalate"'
    if ok:
        out_file = tmp / "decide-out.json"
        rc, out = _run(
            [
                str(SCRIPTS / "jev.py"),
                "decide",
                str(answers),
                "--out",
                str(out_file),
            ]
        )
        try:
            payload = json.loads(out_file.read_text(encoding="utf-8"))
            ok = rc == 0 and "decision" in payload
        except (OSError, ValueError):
            ok = False
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "jev.py"),
                "decide",
                str(answers),
                "--jq",
                "nope.nope",
            ]
        )
        ok = rc == 2
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
            # answer the request's own question names so response
            # validation passes for both ask (choice q) and ping (noul ok)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                request = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, TypeError):
                request = {}
            questions = request.get("questions") or {}
            if "ok" in questions:
                reply = json.dumps(
                    {
                        "answers": {
                            "ok": {"type": "noul", "noul": 0.99}
                        },
                        "model": "smoke-stub",
                    }
                ).encode("utf-8")
            else:
                reply = body
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

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
        if ok:
            # ping --json hits the same stub: {ok, model, noul, ms}
            thread = threading.Thread(
                target=server.handle_request, daemon=True
            )
            thread.start()
            rc, out = _run(
                [str(SCRIPTS / "jev.py"), "ping", "--json"], env=env
            )
            if rc == 0:
                try:
                    ok = json.loads(out.strip()).get("ok") is True
                except ValueError:
                    ok = False
            else:
                ok = False
        if ok:
            # ask --jq digs one response field; --out persists the payload
            thread = threading.Thread(
                target=server.handle_request, daemon=True
            )
            thread.start()
            out_file = tmp / "ask-out.json"
            rc, out = _run(
                [
                    str(SCRIPTS / "jev.py"),
                    "ask",
                    str(request),
                    "--jq",
                    "answers.q.choice",
                    "--out",
                    str(out_file),
                ],
                env=env,
            )
            ok = rc == 0 and out.strip().splitlines()[0] == '"a"'
            try:
                ok = ok and "answers" in json.loads(
                    out_file.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                ok = False
        if ok:
            # a bad --jq key exits 2 (the stub still serves one more ask)
            thread = threading.Thread(
                target=server.handle_request, daemon=True
            )
            thread.start()
            rc, out = _run(
                [
                    str(SCRIPTS / "jev.py"),
                    "ask",
                    str(request),
                    "--jq",
                    "nope.nope",
                ],
                env=env,
            )
            ok = rc == 2
        if ok:
            # ask fails open when the endpoint is unreachable (port 1 is
            # never listening)
            bad_policy = tmp / "policy-down.json"
            policy["endpoint"] = "http://127.0.0.1:1/v1/systemone"
            bad_policy.write_text(json.dumps(policy), encoding="utf-8")
            env["JEV_POLICY"] = str(bad_policy)
            rc, out = _run(
                [str(SCRIPTS / "jev.py"), "ask", str(request)],
                env=env,
            )
            ok = rc != 0 and "network error" in out
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
    if ok:
        # --status --jq digs one field; a bad key exits 2
        rc, out = _run(
            [
                str(SCRIPTS / "peer_fill.py"),
                "--status",
                "--cwd",
                str(tmp / "cwd"),
                "--jq",
                "miss",
            ]
        )
        ok = rc == 0 and out.strip() in ("true", "false")
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "peer_fill.py"),
                    "--status",
                    "--cwd",
                    str(tmp / "cwd"),
                    "--jq",
                    "nope.nope",
                ]
            )
            ok = rc == 2
    if ok:
        # --watch ticks the fill state; --verdict writes the slim probe
        verdict = tmp / "peer-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "peer_fill.py"),
                "--cwd",
                str(tmp / "cwd"),
                "--watch",
                "0.05",
                "--max-ticks",
                "2",
                "--verdict",
                str(verdict),
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"miss"' in ln]
        ok = rc in (0, 1) and len(ticks) == 2
        try:
            ok = ok and "verdict" in json.loads(verdict.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ok = False
    if ok:
        # --quiet keeps clean ticks off stdout (stderr still logs them)
        clean_cwd = tmp / "peer-clean-cwd"
        clean_cwd.mkdir(parents=True, exist_ok=True)
        rc, out = _run(
            [
                str(SCRIPTS / "peer_fill.py"),
                "--cwd",
                str(clean_cwd),
                "--watch",
                "0.05",
                "--max-ticks",
                "2",
                "--quiet",
            ]
        )
        stdout_ticks = [ln for ln in out.splitlines() if '"miss"' in ln]
        ok = rc in (0, 1) and not stdout_ticks and "watch tick=2" in out
    if ok:
        # --fail-fast stops the watch on the first tick with a pending miss
        miss_cwd = tmp / "miss-cwd"
        miss_cwd.mkdir(parents=True, exist_ok=True)
        (miss_cwd / ".jev-tools-miss.json").write_text(
            json.dumps({"task": "smoke", "written_at": time.time()}),
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "peer_fill.py"),
                "--cwd",
                str(miss_cwd),
                "--watch",
                "0.05",
                "--max-ticks",
                "5",
                "--fail-fast",
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"miss"' in ln]
        ok = len(ticks) == 1 and "watch tick=2" not in out
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
    ("install", "step_install"),
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

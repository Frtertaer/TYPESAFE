#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""drift_calibrate.py — drift streak -> calibration PR plan, offline dry-runs.

Fixtures stand in for the live-eval workflow artifacts: eval-live.json
(drift.streaks + case rows), eval-history.jsonl, a decisions.jsonl the CI
run produced, and a copied policy.json. Asserts the action ladder —
skip / dedupe / comment / open_pr — that a verdict-backed-flips gate
drives open_pr, that the real policy file is never written, and that
--dry-run composes the plan without touching --out-dir.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import drift_calibrate


def run_cli(*args: str, env_add: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_add or {})
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "drift_calibrate.py"), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def write_jsonl(path: Path, rows: list) -> None:
    path.write_text(
        "\n".join(json.dumps(r) if isinstance(r, dict) else r for r in rows)
        + "\n",
        encoding="utf-8",
    )


def write_policy(path: Path, **over) -> bytes:
    policy = {
        "version": 3,
        "confidence_floor": 0.3,
        "strong_pick": 0.85,
        "tight_gap": 0.08,
        "noul_yes": 0.7,
        "streak_warn_weeks": 2,
        "streak_fail_weeks": 3,
        "calibrate_min_entries": 5,
        "calibrate_min_flips": 1,
    }
    policy.update(over)
    path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
    return path.read_bytes()


def write_eval(path: Path, streaks: dict, rows=None) -> None:
    path.write_text(
        json.dumps(
            {
                "drift": {
                    "streaks": streaks,
                    "flags": ["warn flags"],
                    "fails": [c for c, n in streaks.items() if n >= 3],
                    "warn_weeks": 2,
                    "fail_weeks": 3,
                },
                "rows": rows or [],
            }
        ),
        encoding="utf-8",
    )


def flip_entry(i: int, outcome="applied") -> dict:
    """conf 0.2 sits below the shipped 0.30 floor but above every candidate
    floor <=0.20, and the 0.90 top prob clears strong_pick — so any calibrate
    recommendation that relaxes the floor flips this escalate -> winner."""
    return {
        "ts": 1700000000.0 + i,
        "schema": 2,
        "harness": "live-eval",
        "prompt_sha": "flip%06d" % i,
        "prompt_head": "eval prompt %d" % i,
        "jev_status": "escalate",
        "probabilities": {"demo": 0.9, "other": 0.1},
        "pick_confidence": 0.2,
        "strong_pick": True,
        "shortlist": ["demo", "other"],
        "outcome": outcome,
    }


class GateTest(unittest.TestCase):
    def test_skip_when_no_streak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pol = tmp / "policy.json"
            write_policy(pol)
            ev = tmp / "eval-live.json"
            write_eval(ev, {"case-a": 1})
            out_dir = tmp / "cal"
            proc = run_cli(
                "--eval", str(ev), "--policy", str(pol),
                "--out-dir", str(out_dir),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            plan = json.loads((out_dir / "plan.json").read_text())
            self.assertEqual(plan["action"], "skip")

    def test_skip_when_streak_below_fail_weeks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pol = tmp / "policy.json"
            write_policy(pol)
            ev = tmp / "eval-live.json"
            write_eval(ev, {"case-a": 2})
            out_dir = tmp / "cal"
            run_cli("--eval", str(ev), "--policy", str(pol),
                    "--out-dir", str(out_dir))
            plan = json.loads((out_dir / "plan.json").read_text())
            self.assertEqual(plan["action"], "skip")

    def test_streak_from_history_fallback(self) -> None:
        """No eval payload -> the newest history record's streaks apply."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pol = tmp / "policy.json"
            write_policy(pol)
            hist = tmp / "eval-history.jsonl"
            write_jsonl(
                hist,
                [{"ts": 1, "verdict": "FAIL", "streaks": {"case-b": 4}}],
            )
            log = tmp / "decisions.jsonl"
            write_jsonl(log, [flip_entry(i) for i in range(6)])
            out_dir = tmp / "cal"
            proc = run_cli(
                "--history", str(hist), "--decisions", str(log),
                "--policy", str(pol), "--out-dir", str(out_dir),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            plan = json.loads((out_dir / "plan.json").read_text())
            self.assertEqual(plan["streak_cases"], {"case-b": 4})
            self.assertIn(plan["action"], ("open_pr", "comment"))

    def test_dedupe_when_calibration_pr_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pol = tmp / "policy.json"
            write_policy(pol)
            ev = tmp / "eval-live.json"
            write_eval(ev, {"case-a": 5})
            open_prs = tmp / "prs.json"
            open_prs.write_text(
                json.dumps(
                    [
                        {
                            "number": 7,
                            "title": "calibration: drift thresholds",
                            "url": "https://example.test/pull/7",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            log = tmp / "decisions.jsonl"
            write_jsonl(log, [flip_entry(i) for i in range(6)])
            out_dir = tmp / "cal"
            proc = run_cli(
                "--eval", str(ev), "--decisions", str(log),
                "--policy", str(pol), "--open-prs", str(open_prs),
                "--out-dir", str(out_dir),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            plan = json.loads((out_dir / "plan.json").read_text())
            self.assertEqual(plan["action"], "dedupe")
            self.assertIn("pull/7", plan["reason"])
            self.assertFalse((out_dir / "pr-body.md").exists())

    def test_comment_when_log_thin(self) -> None:
        """Streak hits the fail gate but the CI log is too thin -> comment."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pol = tmp / "policy.json"
            write_policy(pol, calibrate_min_entries=5)
            ev = tmp / "eval-live.json"
            write_eval(ev, {"case-a": 3})
            log = tmp / "decisions.jsonl"
            write_jsonl(log, [flip_entry(0)])
            out_dir = tmp / "cal"
            proc = run_cli(
                "--eval", str(ev), "--decisions", str(log),
                "--policy", str(pol), "--out-dir", str(out_dir),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            plan = json.loads((out_dir / "plan.json").read_text())
            self.assertEqual(plan["action"], "comment")
            self.assertIn("insufficient data", plan["reason"])
            comment = (out_dir / "comment.md").read_text(encoding="utf-8")
            self.assertIn("case-a", comment)
            self.assertFalse((out_dir / "pr-body.md").exists())

    def test_comment_when_no_verdicted_flips(self) -> None:
        """Flips without an `outcome` verdict never open a PR."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pol = tmp / "policy.json"
            write_policy(pol, calibrate_min_entries=5)
            ev = tmp / "eval-live.json"
            write_eval(ev, {"case-a": 3})
            log = tmp / "decisions.jsonl"
            rows = [flip_entry(i, outcome="") for i in range(6)]
            for r in rows:
                del r["outcome"]
            write_jsonl(log, rows)
            out_dir = tmp / "cal"
            proc = run_cli(
                "--eval", str(ev), "--decisions", str(log),
                "--policy", str(pol), "--out-dir", str(out_dir),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            plan = json.loads((out_dir / "plan.json").read_text())
            self.assertEqual(plan["action"], "comment")
            self.assertIn("no verdict-backed flips", plan["reason"])
            self.assertFalse((out_dir / "pr-body.md").exists())


class OpenPrTest(unittest.TestCase):
    def _fixtures(self, tmp: Path, n: int = 8):
        pol = tmp / "policy.json"
        before = write_policy(pol, calibrate_min_entries=5,
                              calibrate_min_flips=1)
        ev = tmp / "eval-live.json"
        write_eval(ev, {"noul-budget": 4})
        log = tmp / "decisions.jsonl"
        write_jsonl(log, [flip_entry(i) for i in range(n)])
        return pol, before, ev, log

    def test_open_pr_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pol, before, ev, log = self._fixtures(tmp)
            out_dir = tmp / "cal"
            proc = run_cli(
                "--eval", str(ev), "--decisions", str(log),
                "--policy", str(pol), "--out-dir", str(out_dir),
                "--run-url", "https://example.test/runs/9",
                "--branch", "calibration/test", "--label", "calibration",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            plan = json.loads((out_dir / "plan.json").read_text())
            self.assertEqual(plan["action"], "open_pr", plan["reason"])
            self.assertGreater(plan["verdicted_flips"], 0)
            self.assertEqual(plan["pr"]["branch"], "calibration/test")
            self.assertEqual(plan["pr"]["label"], "calibration")
            body = (out_dir / "pr-body.md").read_text(encoding="utf-8")
            self.assertIn("verdict-backed flips", body)
            self.assertIn("noul-budget", body)
            self.assertIn("confidence_floor", body)
            self.assertIn("policy.json diff", body)
            diff = (out_dir / "policy.diff").read_text(encoding="utf-8")
            self.assertIn("-", diff)
            patched = json.loads(
                (out_dir / "policy.json").read_text(encoding="utf-8")
            )
            self.assertNotEqual(
                patched.get("confidence_floor"), 0.3,
                "patched copy must carry the recommended floor",
            )
            # the source policy file is never rewritten
            self.assertEqual(pol.read_bytes(), before)

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pol, _before, ev, log = self._fixtures(tmp)
            out_dir = tmp / "cal"
            proc = run_cli(
                "--eval", str(ev), "--decisions", str(log),
                "--policy", str(pol), "--out-dir", str(out_dir),
                "--dry-run",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            plan = json.loads(proc.stdout)
            self.assertEqual(plan["action"], "open_pr")
            self.assertTrue(plan["dry_run"])
            self.assertFalse(out_dir.exists())

    def test_min_entries_flag_overrides_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pol = tmp / "policy.json"
            write_policy(pol, calibrate_min_entries=50)
            ev = tmp / "eval-live.json"
            write_eval(ev, {"case-a": 3})
            log = tmp / "decisions.jsonl"
            write_jsonl(log, [flip_entry(i) for i in range(3)])
            out_dir = tmp / "cal"
            proc = run_cli(
                "--eval", str(ev), "--decisions", str(log),
                "--policy", str(pol), "--out-dir", str(out_dir),
                "--min-entries", "3", "--min-flips", "1",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            plan = json.loads((out_dir / "plan.json").read_text())
            self.assertEqual(plan["min_entries"], 3)
            self.assertIn(plan["action"], ("open_pr", "comment"))


class CliSurfaceTest(unittest.TestCase):
    def test_json_jq_schema_env_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pol = tmp / "policy.json"
            write_policy(pol)
            ev = tmp / "eval-live.json"
            write_eval(ev, {"case-a": 1})
            out_dir = tmp / "cal"
            proc = run_cli(
                "--eval", str(ev), "--policy", str(pol),
                "--out-dir", str(out_dir), "--json",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            plan = json.loads(proc.stdout)
            self.assertEqual(plan["action"], "skip")
            jq = run_cli(
                "--eval", str(ev), "--policy", str(pol),
                "--out-dir", str(out_dir), "--jq", "action",
            )
            self.assertEqual(jq.returncode, 0)
            self.assertEqual(json.loads(jq.stdout), "skip")
            bad = run_cli(
                "--eval", str(ev), "--policy", str(pol),
                "--out-dir", str(out_dir), "--jq", "nope",
            )
            self.assertEqual(bad.returncode, 2)
            schema = run_cli("--schema")
            self.assertEqual(schema.returncode, 0)
            self.assertIn(":", schema.stdout.splitlines()[0])
            env = run_cli("--env")
            self.assertEqual(env.returncode, 0)
            for key in ("eval", "history", "decisions", "policy",
                        "open_prs", "out_dir", "branch", "label",
                        "min_entries", "min_flips"):
                self.assertIn(key, json.loads(env.stdout))
            verdict = tmp / "verdict.json"
            run_cli(
                "--eval", str(ev), "--policy", str(pol),
                "--out-dir", str(out_dir), "--verdict", str(verdict),
            )
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "ok")
            self.assertEqual(payload["action"], "skip")

    def test_self_test(self) -> None:
        proc = run_cli("--self-test")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("self-test: ok", proc.stdout)

    def test_help_rc0(self) -> None:
        proc = run_cli("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("usage", proc.stdout.lower())

    def test_empty_eval_file_is_fail_open(self) -> None:
        """Garbage eval JSON -> empty streaks -> skip, not a crash."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pol = tmp / "policy.json"
            write_policy(pol)
            ev = tmp / "eval-live.json"
            ev.write_text("{not json", encoding="utf-8")
            out_dir = tmp / "cal"
            proc = run_cli(
                "--eval", str(ev), "--policy", str(pol),
                "--out-dir", str(out_dir),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            plan = json.loads((out_dir / "plan.json").read_text())
            self.assertEqual(plan["action"], "skip")


if __name__ == "__main__":
    unittest.main()

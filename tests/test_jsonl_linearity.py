#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""--jsonl contract: every non-blank stdout line parses as one JSON
object (dict) — no pretty blocks, no bare scalars."""
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


def run(argv: list, cwd: str, env: dict | None = None, stdin: str = "") -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e["JEV_CONSULT_LOG"] = "0"
    e.setdefault("TYPESAFE_API_KEY", "test-key")
    if env:
        e.update(env)
    return subprocess.run(
        argv, input=stdin, capture_output=True, text=True,
        timeout=120, cwd=cwd, env=e,
    )


def assert_jsonl(test: unittest.TestCase, stdout: str) -> None:
    lines = [l for l in stdout.splitlines() if l.strip()]
    test.assertTrue(lines, "empty --jsonl output")
    for line in lines:
        payload = json.loads(line)
        test.assertIsInstance(payload, dict, "non-dict jsonl row: %r" % line[:80])


class JsonlLinearityTests(unittest.TestCase):
    def test_lint_findings_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text('{"policy": {}}', encoding="utf-8")
            q = Path(tmp) / "q.json"
            q.write_text(
                '{"questions":{"q1":{"type":"choice",'
                '"instructions":"x","criteria":{"a":"y"}}}}',
                encoding="utf-8",
            )
            skl = Path(tmp) / "skl"
            skl.mkdir()
            (skl / "SKILL.md").write_text("# x\n", encoding="utf-8")
            cases = {
                "policy_lint": [str(bad)],
                "question_lint": [str(q)],
                "skill_lint": [str(skl)],
                "trigger_lint": [str(bad)],
            }
            for lint, argv_files in cases.items():
                with self.subTest(script=lint):
                    proc = run(
                        [sys.executable, str(SCRIPTS / (lint + ".py")),
                         *argv_files, "--jsonl"],
                        tmp,
                    )
                    self.assertIn(proc.returncode, (0, 1), proc.stderr[:200])
                    assert_jsonl(self, proc.stdout)

    def test_trace_rows_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / "t.json"
            run([sys.executable, str(SCRIPTS / "trace.py"),
                 "--file", str(trace), "init", "--plan", "p"], tmp)
            run([sys.executable, str(SCRIPTS / "trace.py"),
                 "--file", str(trace), "record", "--pick", "x"], tmp)
            for sub in (["history", "--jsonl"], ["schema", "--jsonl"],
                        ["export", "--jsonl"]):
                with self.subTest(subcommand=" ".join(sub)):
                    proc = run(
                        [sys.executable, str(SCRIPTS / "trace.py"),
                         "--file", str(trace), *sub],
                        tmp,
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr[:200])
                    assert_jsonl(self, proc.stdout)

    def test_decisions_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            log.write_text(
                json.dumps(
                    {"ts": 1.0, "harness": "x", "jev_status": "none",
                     "outcome": "miss", "fill": "none", "winner": None,
                     "need": 0.1}
                )
                + "\n",
                encoding="utf-8",
            )
            proc = run(
                [sys.executable, str(SCRIPTS / "decisions.py"), "--jsonl"],
                tmp,
                env={"JEV_DECISIONS": str(log)},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[:200])
            assert_jsonl(self, proc.stdout)

    def test_inventory_hook_events_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(
                [sys.executable, str(SCRIPTS / "inventory_hook.py"),
                 "--events", "--jsonl"],
                tmp,
            )
            self.assertEqual(proc.returncode, 0)
            assert_jsonl(self, proc.stdout)

    def test_doctor_checks_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(
                [sys.executable, str(SCRIPTS / "doctor.py"), "--jsonl"],
                tmp,
            )
            self.assertIn(proc.returncode, (0, 1))
            assert_jsonl(self, proc.stdout)


if __name__ == "__main__":
    unittest.main()

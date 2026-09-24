#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Watch-mode contract: every --watch tick on stdout is one line of JSON
containing a ``ts`` field. Guards against a loop regressing to pretty
dumps or bare text lines."""
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
HARNESS = ROOT / "tests" / "fixtures" / "inventory-harness"


def run(script: str, argv: list, env: dict | None = None, cwd: str | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e.pop("JEV_CONSULT_LOG", None)
    e["JEV_CONSULT_LOG"] = "0"
    if env:
        e.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *argv],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=cwd or str(ROOT),
        env=e,
    )


def assert_ticks(test: unittest.TestCase, proc: subprocess.CompletedProcess) -> None:
    test.assertIn(proc.returncode, (0, 1), proc.stderr[:300])
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    test.assertTrue(lines, "no ticks emitted")
    for line in lines:
        tick = json.loads(line)
        test.assertIsInstance(tick, dict)
        test.assertIn("ts", tick)


class WatchTickShapeTests(unittest.TestCase):
    def test_trace_state_watch_ticks_are_jsonl_with_ts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / ".jev-trace.json"
            trace.write_text(
                json.dumps(
                    {"plan": "p", "current_step": "s", "attempt_count": 0,
                     "last_error": "", "unknown": "", "inspected": [],
                     "last_pick": "", "history": [], "notes": []}
                ),
                encoding="utf-8",
            )
            proc = run(
                "trace.py",
                ["--file", str(trace), "state", "--watch", "0.05",
                 "--max-ticks", "2"],
            )
        assert_ticks(self, proc)

    def test_decisions_watch_ticks_are_jsonl_with_ts(self) -> None:
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
                "decisions.py",
                ["--watch", "0.05", "--max-ticks", "2"],
                env={"JEV_DECISIONS": str(log)},
            )
        assert_ticks(self, proc)

    def test_policy_lint_watch_ticks_are_jsonl_with_ts(self) -> None:
        proc = run(
            "policy_lint.py", ["--watch", "0.05", "--max-ticks", "2"]
        )
        assert_ticks(self, proc)

    def test_doctor_watch_ticks_are_jsonl_with_ts(self) -> None:
        proc = run(
            "doctor.py",
            ["--watch", "0.05", "--max-ticks", "2"],
            cwd=str(HARNESS),
        )
        assert_ticks(self, proc)

    def test_compare_watch_ticks_are_jsonl_with_ts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cases = Path(tmp) / "cases.json"
            cases.write_text(
                json.dumps(
                    {"cases": [{"id": "c1", "expect": {"a": 0.9},
                                "sides": {"a": "model-a"}}]}
                ),
                encoding="utf-8",
            )
            proc = run(
                "compare.py",
                ["--cases", str(cases), "--watch", "0.05",
                 "--max-ticks", "2"],
            )
        assert_ticks(self, proc)

    def test_compact_watch_ticks_are_jsonl_with_ts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "transcript.json"
            transcript.write_text(
                json.dumps(
                    {"messages": [{"role": "user", "text": "hi"},
                                  {"role": "assistant", "text": "ok"}]}
                ),
                encoding="utf-8",
            )
            proc = run(
                "compact.py",
                [str(transcript), "--history", "--watch", "0.05",
                 "--max-ticks", "2"],
            )
        assert_ticks(self, proc)

    def test_progress_status_watch_ticks_are_jsonl_with_ts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            db = Path(tmp) / "ledger.db"
            proc = run(
                "progress.py",
                ["--repo", str(repo), "--db", str(db),
                 "status", "impl", "--watch", "0.05",
                 "--max-ticks", "2"],
            )
        assert_ticks(self, proc)

    def test_inventory_watch_emits_parseable_json_ticks(self) -> None:
        """inventory --watch prints the one-shot pretty payload first,
        then JSONL ticks — pin both halves."""
        proc = run(
            "inventory.py",
            ["--task", "jwt", "--watch", "0.05", "--max-ticks", "2"],
            cwd=str(HARNESS),
        )
        self.assertIn(proc.returncode, (0, 1), proc.stderr[:300])
        lines = proc.stdout.splitlines()
        boundary = next(
            i for i, line in enumerate(lines) if line.rstrip() == "}"
        )
        head = json.loads("\n".join(lines[: boundary + 1]))
        self.assertIn("counts", head)
        tick_lines = [l for l in lines[boundary + 1 :] if l.strip()]
        self.assertTrue(tick_lines)
        for line in tick_lines:
            tick = json.loads(line)
            self.assertIsInstance(tick, dict)
            self.assertIn("ts", tick)


if __name__ == "__main__":
    unittest.main()

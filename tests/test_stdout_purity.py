#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""stdout purity pin: payload emitters keep diagnostics off stdout.

When --out writes rows/payloads to a file, any "wrote ..."/"watch tick"
style diagnostic must land on stderr — stdout holds the payload only
(either the same content, or empty per the script's convention)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

DIAG = re.compile(r"^(wrote |watch tick|\d+ entr|scanning |baseline:)")


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
        timeout=120,
        cwd=cwd or str(ROOT),
        env=e,
    )


def assert_no_stdout_diagnostics(test: unittest.TestCase, proc: subprocess.CompletedProcess) -> None:
    bad = [l for l in proc.stdout.splitlines() if DIAG.match(l.strip())]
    test.assertEqual(bad, [], "diagnostics leaked to stdout: %r" % bad)


class StdoutPurityTests(unittest.TestCase):
    def test_decisions_out_keeps_diagnostics_off_stdout(self) -> None:
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
            out = Path(tmp) / "rows.csv"
            proc = run(
                "decisions.py",
                ["--csv", "--out", str(out)],
                env={"JEV_DECISIONS": str(log)},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[:300])
            assert_no_stdout_diagnostics(self, proc)
            self.assertIn("wrote", proc.stderr)
            self.assertTrue(out.is_file())

    def test_trace_notes_out_keeps_diagnostics_off_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / ".jev-trace.json"
            trace.write_text(
                json.dumps(
                    {"plan": "", "current_step": "", "attempt_count": 0,
                     "last_error": "", "unknown": "", "inspected": [],
                     "last_pick": "",
                     "history": [{"ts": 1000, "iso": "2026-01-01T00:00:00Z",
                                  "pick": "a"}],
                     "notes": [{"ts": 1000, "iso": "2026-01-01T00:00:00Z",
                                "text": "n", "sha": "s"}]}
                ),
                encoding="utf-8",
            )
            out = Path(tmp) / "notes.csv"
            proc = run(
                "trace.py",
                ["--file", str(trace), "notes", "--csv",
                 "--out", str(out)],
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[:300])
            assert_no_stdout_diagnostics(self, proc)
            self.assertTrue(out.is_file())

    def test_smoke_out_keeps_diagnostics_off_stdout(self) -> None:
        """smoke tees rows to stdout AND --out; the 'wrote' note must
        still be stderr-only."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "steps.csv"
            proc = run(
                "smoke.py",
                ["--only", "policy", "--csv", "--out", str(out)],
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[:300])
            assert_no_stdout_diagnostics(self, proc)
            self.assertIn("wrote", proc.stderr)
            self.assertTrue(out.is_file())

    def test_lint_json_stdout_is_pure_payload(self) -> None:
        """lint --json stdout parses as a findings array/object only."""
        proc = run("policy_lint.py", ["--json"])
        self.assertIn(proc.returncode, (0, 1), proc.stderr[:300])
        assert_no_stdout_diagnostics(self, proc)
        json.loads(proc.stdout)

    def test_wrote_diagnostics_are_stderr_in_source(self) -> None:
        """Every 'wrote ...' emit must be a stderr write, never stdout/print."""
        bad = []
        for path in sorted(SCRIPTS.glob("*.py")):
            lines = path.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                if '"wrote ' not in line and "'wrote " not in line:
                    continue
                ctx = "\n".join(lines[max(0, i - 4):i + 1])
                if "stderr" not in ctx:
                    bad.append("%s: %s" % (path.name, line.strip()[:80]))
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()

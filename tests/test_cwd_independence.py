#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Scripts resolve bundled resources via __file__, not the cwd.

Run a battery of no-arg/default-arg commands from a foreign cwd and
assert they do not crash on missing files (traceback / FileNotFound).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"


def run_from_tmp(script: str, argv: list) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ)
        env["JEV_CONSULT_LOG"] = "0"
        env["JEV_DECISIONS"] = str(Path(tmp) / "decisions.jsonl")
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / script), *argv],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=tmp,
            env=env,
        )
    return proc


class CwdIndependenceTests(unittest.TestCase):
    def assert_no_traceback(self, proc: subprocess.CompletedProcess) -> None:
        blob = proc.stderr + proc.stdout
        self.assertNotIn("Traceback", blob[:2000])
        self.assertNotIn("FileNotFoundError", blob[:2000])

    def test_policy_lint_default_policy(self) -> None:
        proc = run_from_tmp("policy_lint.py", [])
        self.assertIn(proc.returncode, (0, 1, 2))
        self.assert_no_traceback(proc)

    def test_compare_default_cases(self) -> None:
        proc = run_from_tmp("compare.py", [])
        self.assertIn(proc.returncode, (0, 1, 2))
        self.assert_no_traceback(proc)

    def test_smoke_policy_step(self) -> None:
        proc = run_from_tmp("smoke.py", ["--only", "policy"])
        self.assertIn(proc.returncode, (0, 1, 2))
        self.assert_no_traceback(proc)

    def test_decisions_empty_log(self) -> None:
        proc = run_from_tmp("decisions.py", ["--json"])
        self.assertIn(proc.returncode, (0, 1, 2))
        self.assert_no_traceback(proc)

    def test_doctor_env_only(self) -> None:
        proc = run_from_tmp("doctor.py", ["--env"])
        self.assertEqual(proc.returncode, 0)
        self.assert_no_traceback(proc)

    def test_inventory_empty_dir(self) -> None:
        proc = run_from_tmp("inventory.py", ["--json"])
        self.assertIn(proc.returncode, (0, 1, 2))
        self.assert_no_traceback(proc)


if __name__ == "__main__":
    unittest.main()

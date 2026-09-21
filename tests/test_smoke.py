#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for skills/jev-consult/scripts/smoke.py."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "skills" / "jev-consult" / "scripts" / "smoke.py"
SPEC = importlib.util.spec_from_file_location("jev_smoke", SMOKE)
MOD = importlib.util.module_from_spec(SPEC)
sys.modules["jev_smoke"] = MOD
SPEC.loader.exec_module(MOD)


class SmokeTests(unittest.TestCase):
    def test_full_smoke_green(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SMOKE)], capture_output=True, text=True, timeout=120
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["steps"]), 13)
        names = {s["name"] for s in out["steps"]}
        self.assertEqual(
            names,
            {
                "policy",
                "policy_lint",
                "jev_scaffold_lint",
                "inventory",
                "compact_fake",
                "decisions",
                "trace",
                "skill_lint",
                "question_lint",
                "compare",
                "apply_fill",
                "hook",
                "doctor_json",
            },
        )

    def test_only_runs_subset(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--only", "policy,trace"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out["ok"])
        self.assertEqual({s["name"] for s in out["steps"]}, {"policy", "trace"})

    def test_only_unknown_step_rc2(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--only", "bogus"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unknown step", proc.stderr)

    def test_only_doctor_json_uses_emitted_name(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--only", "doctor_json"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = json.loads(proc.stdout)
        self.assertEqual({s["name"] for s in out["steps"]}, {"doctor_json"})

    def test_step_failure_marks_not_ok(self) -> None:
        def boom(tmp):
            raise RuntimeError("explode")

        with patch.object(MOD, "_run", return_value=(1, "nope")), patch.object(
            MOD, "step_policy", side_effect=boom
        ):
            import io

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = MOD.main([])
        self.assertEqual(rc, 1)
        out = json.loads(buf.getvalue())
        self.assertFalse(out["ok"])
        self.assertIn("explode", out["steps"][0]["detail"])

    def test_policy_step_real(self) -> None:
        from pathlib import Path as P
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            step = MOD.step_policy(P(tmp))
        self.assertTrue(step["ok"], step)


if __name__ == "__main__":
    unittest.main(verbosity=2)

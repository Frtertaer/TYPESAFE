#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A corrupt or malformed JEV_POLICY must never traceback.

Scripts that need the policy exit 1 with a clean one-line error;
hooks (and other fail-open paths) emit {} and exit 0.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"


def run(script: str, argv: list[str], env: dict, inp: str | None = None):
    merged = dict(os.environ)
    merged.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script)] + argv,
        input=inp,
        capture_output=True,
        text=True,
        env=merged,
        timeout=30,
    )


class CorruptPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        bad = Path(self.tmp.name) / "bad-policy.json"
        bad.write_text("{broken json", encoding="utf-8")
        self.env = {"JEV_POLICY": str(bad)}

    def test_jev_scaffold_clean_error(self) -> None:
        out_path = Path(self.tmp.name) / "req.json"
        proc = run(
            "jev.py",
            ["scaffold", "x", "--out", str(out_path)],
            self.env,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not JSON", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertFalse(out_path.exists())

    def test_jev_non_object_policy(self) -> None:
        arr = Path(self.tmp.name) / "arr-policy.json"
        arr.write_text("[]", encoding="utf-8")
        proc = run(
            "jev.py",
            ["scaffold", "x"],
            {"JEV_POLICY": str(arr)},
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("must be an object", proc.stderr)

    def test_inventory_hook_fails_open(self) -> None:
        payload = (
            '{"hook_event_name": "UserPromptSubmit", "prompt": "x", "cwd": "."}'
        )
        env = dict(self.env)
        env["JEV_CONSULT_LOG"] = "0"
        proc = run("inventory_hook.py", [], env, inp=payload)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("{}", proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)

    def test_compact_hook_fails_open(self) -> None:
        proc = run("compact_hook.py", [], self.env, inp="{}")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("{}", proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)

    def test_decisions_runs_without_policy(self) -> None:
        log = Path(self.tmp.name) / "decisions.jsonl"
        log.write_text('{"jev_status": "ok"}\n', encoding="utf-8")
        env = dict(self.env)
        env["JEV_DECISIONS"] = str(log)
        proc = run("decisions.py", ["--count"], env)
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)

    def test_policy_lint_reports_error(self) -> None:
        proc = run(
            "policy_lint.py",
            [str(Path(self.tmp.name) / "bad-policy.json")],
            self.env,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main()

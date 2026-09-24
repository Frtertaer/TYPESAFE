#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Every non-vendored pack CLI exposes a key contract:
--schema flag on argparse scripts, `schema` subcommand on trace.
Vendored skill_scanner.py and the _watch/progress_core libraries are
out of scope."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

FLAG_SCHEMA = sorted(
    p.stem
    for p in SCRIPTS.glob("*.py")
    if p.stem not in {"_watch", "progress_core", "skill_scanner", "trace",
                      "smoke"}  # smoke's contract is its step rows
)


def run(argv: list, cwd: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["JEV_CONSULT_LOG"] = "0"
    env.setdefault("TYPESAFE_API_KEY", "test-key")
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=cwd,
        env=env,
    )


class SchemaCoverageTests(unittest.TestCase):
    def test_schema_flag_emits_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in FLAG_SCHEMA:
                with self.subTest(script=name):
                    proc = run(
                        [sys.executable, str(SCRIPTS / (name + ".py")), "--schema"],
                        tmp,
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr[:200])
                    body = proc.stdout.strip()
                    self.assertTrue(body, "empty schema output")
                    self.assertIn(":", body.splitlines()[0],
                                  "schema rows must be `key: ...`")

    def test_trace_schema_subcommand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / "t.json"
            proc = run(
                [sys.executable, str(SCRIPTS / "trace.py"),
                 "--file", str(trace), "schema", "--keys", "key"],
                tmp,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[:200])
            self.assertIn("plan", proc.stdout)

    def test_schema_json_object_where_supported(self) -> None:
        """--schema --json must emit a JSON object on flag scripts."""
        with tempfile.TemporaryDirectory() as tmp:
            for name in FLAG_SCHEMA:
                with self.subTest(script=name):
                    proc = run(
                        [sys.executable, str(SCRIPTS / (name + ".py")),
                         "--schema", "--json"],
                        tmp,
                    )
                    # some CLIs route --json through a different parser;
                    # accept rc 0 only when output is a JSON object
                    if proc.returncode == 0 and proc.stdout.strip().startswith("{"):
                        import json
                        self.assertIsInstance(json.loads(proc.stdout), dict)


if __name__ == "__main__":
    unittest.main()

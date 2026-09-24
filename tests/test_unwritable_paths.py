#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unwritable/blocked output paths must fail clean: hooks stay rc 0,
--verdict/--out writers report failure on stderr, and nothing leaks a
traceback. A path whose parent is a FILE works on every platform
(no reliance on chmod/read-only semantics)."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"


def blocked_dir(tmp: str) -> str:
    """A directory path that can never exist: its parent is a file."""
    blocker = Path(tmp) / "blocker"
    blocker.write_text("x", encoding="utf-8")
    return str(blocker / "sub")


def run(script: str, argv: list[str], tmp: str, env_extra: dict | None = None,
        stdin: str | None = None):
    env = dict(os.environ)
    env["JEV_CONSULT_LOG"] = "0"
    env.setdefault("TYPESAFE_API_KEY", "test-key")
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script)] + argv,
        capture_output=True, text=True, timeout=120, cwd=tmp, env=env,
        input=stdin,
    )


class UnwritablePathTests(unittest.TestCase):
    def assert_no_traceback(self, proc, tag: str) -> None:
        self.assertNotIn("Traceback", proc.stderr,
                         "%s leaked a traceback: %s" % (tag, proc.stderr[:400]))

    def test_hook_unwritable_cwd_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = run("inventory_hook.py", [], tmp,
                       env_extra={"JEV_HOOK_CWD": blocked_dir(tmp),
                                  "JEV_HOOK_PROMPT": "deploy the proxy"},
                       stdin="{}")
            self.assert_no_traceback(proc, "hook unwritable cwd")
            self.assertEqual(proc.returncode, 0)

    def test_compact_hook_unwritable_spill_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = run("compact_hook.py", [], tmp,
                       env_extra={"JEV_CONSULT_SPILL":
                                  str(Path(blocked_dir(tmp)) / "spill")},
                       stdin='{"messages":[{"role":"u","content":"x"*5000}]}')
            self.assert_no_traceback(proc, "compact_hook unwritable spill")
            self.assertEqual(proc.returncode, 0)

    def test_env_out_to_blocked_path_reports_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(blocked_dir(tmp)) / "env.json")
            proc = run("decisions.py", ["--env", "--out", out], tmp)
            self.assert_no_traceback(proc, "--env --out blocked")
            self.assertIn("cannot write", proc.stderr.lower())
            self.assertEqual(proc.returncode, 0)  # report still delivered

    def test_verdict_to_blocked_path_reports_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            verdict = str(Path(blocked_dir(tmp)) / "v.json")
            log = Path(tmp) / "d.jsonl"
            log.write_text('{"ts":1,"q":"x"}\n', encoding="utf-8")
            proc = run(
                "decisions.py",
                ["--file", str(log), "--count", "--verdict", verdict],
                tmp)
            self.assert_no_traceback(proc, "verdict blocked")
            self.assertIn("failed", proc.stderr.lower())

    def test_decisions_log_at_blocked_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = str(Path(blocked_dir(tmp)) / "decisions.jsonl")
            proc = run("decisions.py", ["--file", log, "--count"], tmp)
            self.assert_no_traceback(proc, "decisions blocked log")


if __name__ == "__main__":
    unittest.main()

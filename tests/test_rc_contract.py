#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Return-code contract:
- argparse CLIs exit 2 on an unknown flag (argparse default).
- hook scripts fail OPEN: unknown flags are ignored, rc 0, payload {} —
  a hook must never break the host tool call."""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

HOOKS = {"compact_hook", "inventory_hook"}
CLIS = sorted(
    p.stem
    for p in SCRIPTS.glob("*.py")
    if p.stem not in {"_watch", "progress_core", "skill_scanner"} | HOOKS
)


def run(script: str, argv: list, stdin: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["JEV_CONSULT_LOG"] = "0"
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *argv],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
        env=env,
    )


class RcContractTests(unittest.TestCase):
    def test_unknown_flag_rc2_on_argparse_clis(self) -> None:
        for name in CLIS:
            with self.subTest(script=name):
                proc = run(name + ".py", ["--definitely-not-a-flag-xyz"])
                self.assertEqual(
                    proc.returncode, 2,
                    "%s returned %r: %s" % (name, proc.returncode, proc.stderr[:200]),
                )

    def test_hooks_fail_open_on_unknown_flag(self) -> None:
        for name in sorted(HOOKS):
            with self.subTest(script=name):
                proc = run(name + ".py", ["--definitely-not-a-flag-xyz"], stdin="{}")
                self.assertEqual(proc.returncode, 0)
                self.assertNotIn("Traceback", proc.stderr[:2000])


if __name__ == "__main__":
    unittest.main()

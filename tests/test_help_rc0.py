#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Every CLI must answer -h/--help with rc 0 and a usage line.

Exercising parser construction catches broken argparse wiring
(conflicting option strings, bad subparser parents, ...)."""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

# Scripts that are imported as libraries / invoked via stdin hooks get a
# pass here only when they have no argparse main; sweep all cli scripts
# that expose argparse (listed in test_script_coverage CLIS style).
CLIS = sorted(p.stem for p in SCRIPTS.glob("*.py") if p.stem not in {
    "_watch", "progress_core",
})


def run(script: str, argv: list) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["JEV_CONSULT_LOG"] = "0"
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *argv],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
        env=env,
    )


class HelpRc0Tests(unittest.TestCase):
    def test_help_flag(self) -> None:
        for name in CLIS:
            with self.subTest(script=name):
                proc = run(name + ".py", ["--help"])
                self.assertEqual(proc.returncode, 0, proc.stderr[:200])
                self.assertIn("usage", (proc.stdout + proc.stderr).lower()[:400])


if __name__ == "__main__":
    unittest.main()

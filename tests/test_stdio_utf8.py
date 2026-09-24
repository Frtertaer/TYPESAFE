#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""fix_stdio coverage pin: every pack CLI reconfigures stdio to utf-8
before it can print — non-ASCII output must never crash on a cp1252
console. Vendored skill_scanner and the _watch/progress_core
libraries are out of scope."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

EXEMPT = {"_watch", "progress_core", "skill_scanner"}  # lib + vendored


class StdioUtf8Tests(unittest.TestCase):
    def test_every_cli_calls_fix_stdio(self) -> None:
        missing = []
        for path in sorted(SCRIPTS.glob("*.py")):
            if path.stem in EXEMPT:
                continue
            src = path.read_text(encoding="utf-8")
            if not re.search(r"fix_stdio\(\)", src):
                missing.append(path.stem)
        self.assertEqual(missing, [])

    def test_fix_stdio_covers_all_three_streams(self) -> None:
        src = (SCRIPTS / "_watch.py").read_text(encoding="utf-8")
        body = src.split("def fix_stdio", 1)[1].split("\ndef ", 1)[0]
        for stream in ("sys.stdout", "sys.stderr", "sys.stdin"):
            self.assertIn(stream, body)
        self.assertIn('"utf-8"', body)
        self.assertIn('errors="replace"', body)


    def test_fix_stdio_pins_lf_newline(self) -> None:
        src = (SCRIPTS / "_watch.py").read_text(encoding="utf-8")
        body = src.split("def fix_stdio", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('newline="\\n"', body)

    def test_emitters_output_raw_lf_bytes(self) -> None:
        """Subprocess-level: on Windows text-mode stdout would emit CRLF;
        the pin must hold for binary consumers (pipes into jq/etc.)."""
        import os
        import subprocess
        import sys
        env = dict(os.environ)
        env["JEV_CONSULT_LOG"] = "0"
        env.setdefault("TYPESAFE_API_KEY", "test-key")
        for argv in (
            ["decisions.py", "--env"],
            ["skill_lint.py", "--version"],
            ["doctor.py", "--version"],
        ):
            with self.subTest(argv=argv):
                proc = subprocess.run(
                    [sys.executable, str(SCRIPTS / argv[0])] + argv[1:],
                    capture_output=True, env=env, timeout=60,
                )
                self.assertNotIn(b"\r", proc.stdout,
                                 "%s emitted CR bytes" % argv[0])


if __name__ == "__main__":
    unittest.main()

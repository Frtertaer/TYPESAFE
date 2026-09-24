#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""--self-test must be a read-only probe: it may create its own temp
fixtures but it must not leave files behind in the caller's cwd."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_selftest_parity import SELF_TESTS

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"


class SelfTestCleanCwd(unittest.TestCase):
    def test_selftest_leaves_no_files(self) -> None:
        for script, (argv_tail, _marker) in sorted(SELF_TESTS.items()):
            with self.subTest(script=script):
                with tempfile.TemporaryDirectory() as cwd:
                    before = set(Path(cwd).iterdir())
                    subprocess.run(
                        [sys.executable, str(SCRIPTS / script)] + argv_tail,
                        capture_output=True,
                        text=True,
                        timeout=60,
                        cwd=cwd,
                    )
                    leftovers = set(Path(cwd).iterdir()) - before
                    self.assertEqual(
                        leftovers, set(),
                        "%s --self-test left %s in cwd"
                        % (script, sorted(p.name for p in leftovers)),
                    )


if __name__ == "__main__":
    unittest.main()

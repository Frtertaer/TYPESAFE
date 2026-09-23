#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Every scripts/*.py module must be referenced by at least one tests/test_*.py.

Word-boundary matching, so `compact_hook` references do not excuse a missing
`compact.py` test. This file is excluded from its own scan.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
TESTS = ROOT / "tests"
SELF = Path(__file__).name


class ScriptTestCoverageTests(unittest.TestCase):
    def test_every_script_referenced_by_some_test(self) -> None:
        blobs = {
            p.name: p.read_text(encoding="utf-8", errors="replace")
            for p in TESTS.glob("test_*.py")
            if p.name != SELF
        }
        missing = []
        for script in sorted(SCRIPTS.glob("*.py")):
            stem = script.stem
            if stem.startswith("__"):
                continue
            pat = re.compile(r"\b" + re.escape(stem) + r"\b")
            if not any(pat.search(blob) for blob in blobs.values()):
                missing.append(script.name)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()

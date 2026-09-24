#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Every add_argument flag in scripts/*.py must be exercised somewhere.

Coverage sources: any tests/test_*.py blob plus smoke.py itself (the
pack's own e2e harness runs flags like --live/--irreversible/--skill).
Private helpers (_watch, progress_core, skill_scanner) and __* modules
are excluded — they carry no CLI surface of their own.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
TESTS = ROOT / "tests"
SMOKE = SCRIPTS / "smoke.py"
SELF = Path(__file__).name
HELPERS = {"_watch", "progress_core", "skill_scanner"}

FLAG = re.compile(
    r'add_argument\(\s*["\'](--[a-z0-9-]+|-[a-zA-Z])["\']'
)


def _covered_blob() -> str:
    blob = "".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in TESTS.glob("test_*.py")
        if p.name != SELF
    )
    return blob + SMOKE.read_text(encoding="utf-8", errors="replace")


class FlagCoverageTests(unittest.TestCase):
    def test_every_flag_referenced_by_some_test_or_smoke(self) -> None:
        blob = _covered_blob()
        missing: dict[str, list[str]] = {}
        for script in sorted(SCRIPTS.glob("*.py")):
            if script.stem.startswith("__") or script.stem in HELPERS:
                continue
            src = script.read_text(encoding="utf-8", errors="replace")
            uncovered = sorted(
                fl for fl in set(FLAG.findall(src)) if fl not in blob
            )
            if uncovered:
                missing[script.name] = uncovered
        self.assertEqual(missing, {})


if __name__ == "__main__":
    unittest.main()

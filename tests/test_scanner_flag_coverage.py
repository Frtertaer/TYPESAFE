#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""skill_scanner.py is exempt from test_flag_coverage (it's listed in
HELPERS because it is a standalone vendored file) — but it exposes a
full CLI of its own. Two drifts this pin closes:

1. Every ``add_argument`` flag in skill_scanner.py must be exercised by
   a literal reference in some tests/test_*.py blob.
2. Every flag listed under MISSING_VALUE_RC2["skill_scanner.py"] must
   actually exist in the script (argparse returns rc 2 for unknown
   flags too, so phantom entries pass vacuously and mask real gaps).
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests.test_missing_value_parity import MISSING_VALUE_RC2

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
TESTS = ROOT / "tests"
SELF = Path(__file__).name

FLAG = re.compile(
    r'add_argument\(\s*["\'](--[a-z0-9-]+|-[a-zA-Z])["\']'
)


def _test_blob() -> str:
    return "".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in TESTS.glob("test_*.py")
        if p.name != SELF
    )


class ScannerFlagCoverageTests(unittest.TestCase):
    def test_every_scanner_flag_is_exercised(self) -> None:
        src = (SCRIPTS / "skill_scanner.py").read_text(encoding="utf-8")
        blob = _test_blob()
        missing = sorted(
            fl for fl in set(FLAG.findall(src)) if fl not in blob
        )
        self.assertEqual(
            missing, [],
            "skill_scanner.py flags with no test reference: %s" % missing,
        )

    def test_missing_value_entries_exist_in_script(self) -> None:
        src = (SCRIPTS / "skill_scanner.py").read_text(encoding="utf-8")
        real = set(FLAG.findall(src))
        phantom = sorted(
            fl for fl in MISSING_VALUE_RC2.get("skill_scanner.py", [])
            if fl not in real
        )
        self.assertEqual(
            phantom, [],
            "MISSING_VALUE_RC2 lists flags skill_scanner.py doesn't parse: %s"
            % phantom,
        )


if __name__ == "__main__":
    unittest.main()

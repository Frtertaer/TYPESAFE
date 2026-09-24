#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Hand-written USAGE blocks must name every flag the script parses.

These six scripts hand-roll argv parsing (no argparse), so a flag can
ship without being documented — pin every "--x" string literal used in
code to appear in the USAGE text."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

USAGE_RE = re.compile(r"^USAGE\s*=\s*'(.*?)'\n", re.S | re.M)
FLAG_RE = re.compile(r'["\'](--[a-z][a-z0-9-]*)["\']')

USAGE_SCRIPTS = [
    "compact_hook.py", "inventory_hook.py", "policy_lint.py",
    "question_lint.py", "skill_lint.py", "trigger_lint.py",
]


class UsageTextTests(unittest.TestCase):
    def test_every_code_flag_is_documented(self) -> None:
        for name in USAGE_SCRIPTS:
            with self.subTest(script=name):
                src = (SCRIPTS / name).read_text(encoding="utf-8")
                m = USAGE_RE.search(src)
                self.assertIsNotNone(m, "%s lost its USAGE block" % name)
                usage = m.group(1)
                code = src.replace(m.group(0), "")
                missing = sorted(
                    f for f in set(FLAG_RE.findall(code)) if f not in usage
                )
                self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()

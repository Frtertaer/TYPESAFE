#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pin the Makefile: every recipe's script path exists, and the
documented entry points are the same commands README/CI use."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"


class MakefilePinsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.src = MAKEFILE.read_text(encoding="utf-8")

    def test_targets_present(self) -> None:
        targets = re.findall(r"^([a-z][a-z-]*):", self.src, re.M)
        for want in ("test", "lint", "smoke", "smoke-quick"):
            self.assertIn(want, targets)

    def test_recipe_scripts_exist(self) -> None:
        scripts = re.findall(r"python (skills/[^\s*]+\.py)", self.src)
        self.assertGreaterEqual(len(scripts), 4)
        for s in scripts:
            with self.subTest(script=s):
                self.assertTrue((ROOT / s).is_file(), s)

    def test_literal_paths_exist(self) -> None:
        for token in re.findall(r"(skills/jev-consult/[^\s*]+|tests/[^\s*]+)", self.src):
            with self.subTest(path=token):
                self.assertTrue((ROOT / token).exists(), token)

    def test_test_target_matches_ci(self) -> None:
        """The `test` target must be the same command CI runs."""
        ci = (ROOT / ".github" / "workflows" / "tests.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("python -m unittest discover -s tests", ci)
        m = re.search(r"^test:.*\n(\t.*)", self.src, re.M)
        self.assertIsNotNone(m)
        self.assertIn("python -m unittest discover -s tests", m.group(1))


if __name__ == "__main__":
    unittest.main()

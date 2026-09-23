#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Script/doc parity: every scripts/*.py file must be named in the README
file table and (vendored snapshots excluded) in SKILL.md, so docs cannot
silently drift from the pack's script inventory."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
README = ROOT / "README.md"
SKILL_MD = ROOT / "skills" / "jev-consult" / "SKILL.md"

# Vendored snapshots live in the scripts dir but are upstream code, not part
# of the pack's own CLI surface — SKILL.md need not document them.
SKILL_EXEMPT = {"skill_scanner.py"}


def script_names() -> list[str]:
    return sorted(path.name for path in SCRIPTS.glob("*.py"))


class ScriptDocParityTests(unittest.TestCase):
    def test_readme_names_every_script(self) -> None:
        readme = README.read_text(encoding="utf-8")
        missing = [name for name in script_names() if name not in readme]
        self.assertEqual(missing, [], "README.md misses script names: %s" % missing)

    def test_skill_md_names_every_nonvendored_script(self) -> None:
        skill = SKILL_MD.read_text(encoding="utf-8")
        missing = [
            name
            for name in script_names()
            if name not in skill and name not in SKILL_EXEMPT
        ]
        self.assertEqual(missing, [], "SKILL.md misses script names: %s" % missing)

    def test_parity_lists_cover_every_script_file(self) -> None:
        # The exemption set must name real files only — a stale entry would
        # silently shrink the SKILL.md contract.
        names = set(script_names())
        stale = SKILL_EXEMPT - names
        self.assertEqual(stale, set(), "exempt names that are not files: %s" % stale)


if __name__ == "__main__":
    unittest.main()

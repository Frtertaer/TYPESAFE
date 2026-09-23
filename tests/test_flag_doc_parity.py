#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Flag doc parity: every quoted ``--flag`` literal in a pack script must
appear in SKILL.md or README.md. Guards flag-vs-doc drift — a new flag has
to land in the docs (or be deliberately exempted below) at write time."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

# Vendored / library / internal files that do not declare user flags.
EXEMPT = {"_watch.py", "skill_scanner.py", "progress_core.py"}
# Universal argparse flag every script gets for free.
ALLOWED = {"--help"}

FLAG_RE = re.compile(r'["\'](--[a-z][a-z-]+)["\']')
DOC_FLAG_RE = re.compile(r"--[a-z][a-z-]+")


def _doc_flags() -> set[str]:
    text = (ROOT / "skills" / "jev-consult" / "SKILL.md").read_text(
        encoding="utf-8"
    ) + "\n" + (ROOT / "README.md").read_text(encoding="utf-8")
    return set(DOC_FLAG_RE.findall(text))


def _script_flags(path: Path) -> set[str]:
    return set(FLAG_RE.findall(path.read_text(encoding="utf-8"))) - ALLOWED


class FlagDocParityTests(unittest.TestCase):
    def test_every_flag_literal_is_documented(self) -> None:
        doc_flags = _doc_flags()
        for path in sorted(SCRIPTS.glob("*.py")):
            if path.name in EXEMPT:
                continue
            with self.subTest(script=path.name):
                missing = sorted(_script_flags(path) - doc_flags)
                self.assertFalse(
                    missing,
                    "%s has undocumented flags %s" % (path.name, missing),
                )

    def test_sanity_core_flags_present(self) -> None:
        doc_flags = _doc_flags()
        for flag in ("--env", "--watch", "--schema", "--jq", "--out"):
            self.assertIn(flag, doc_flags)


if __name__ == "__main__":
    unittest.main()

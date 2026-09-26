#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Harness vocabulary pin: 'hermes', 'claude-code', 'codex', 'grok' is
the only harness set. Any parenthesized sequence listing ≥2 of them in
a script must follow the canonical order; a bare "claude" literal (not
"claude-code") is drift — pinned to the one known self-test site.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

CANON = ("hermes", "claude-code", "codex", "grok")

SEQ_RE = re.compile(r'[\(\[][^\]\)]*"[a-z-]+"[^\]\)]*[\)\]]')
NAME_RE = re.compile(r'"([a-z][a-z-]*)"')

BARE_CLAUDE_OK: dict = {
    "doctor.py": {
        '"claude",  # the claude-code CLI binary name',
    },
}


def _names_in(seq: str) -> list:
    return [n for n in NAME_RE.findall(seq) if n in CANON + ("auto",)]


class HarnessVocabularyTests(unittest.TestCase):
    def test_multi_harness_sequences_use_canonical_order(self) -> None:
        offenders: dict[str, list[str]] = {}
        for script in sorted(SCRIPTS.glob("*.py")):
            if script.stem.startswith("__"):
                continue
            src = script.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(src.splitlines(), 1):
                for seq in SEQ_RE.findall(line):
                    names = [
                        n for n in NAME_RE.findall(seq) if n in CANON
                    ]
                    if len(names) < 2:
                        continue
                    expected = [n for n in CANON if n in names]
                    if names != expected:
                        offenders.setdefault(script.name, []).append(
                            "%d: %s" % (lineno, names)
                        )
        self.assertEqual(offenders, {})

    def test_no_bare_claude_name_outside_known_self_test(self) -> None:
        offenders = []
        for script in sorted(SCRIPTS.glob("*.py")):
            if script.stem.startswith("__"):
                continue
            for lineno, line in enumerate(
                script.read_text(encoding="utf-8").splitlines(), 1
            ):
                if '"claude"' not in line:
                    continue
                if (
                    script.name in BARE_CLAUDE_OK
                    and line.strip() in BARE_CLAUDE_OK[script.name]
                ):
                    continue
                offenders.append("%s:%d %s" % (script.name, lineno, line.strip()))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()

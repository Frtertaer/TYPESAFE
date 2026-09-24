#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Env-var naming pin: scripts read only JEV_*/TYPESAFE_* vars, plus a
small allowlist of platform/home vars needed for harness discovery.
Writes to os.environ are JEV_* only. A new name outside the set fails
here and gets reviewed instead of silently landing."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

# Platform vars legitimately read for home-dir / harness discovery.
ALLOWED = {"HOME", "USERPROFILE", "HERMES_HOME", "SystemRoot"}

READ_RE = re.compile(
    r"(?:os\.environ\.get|os\.getenv|os\.environ\.pop)\(\s*"
    r'["\']([A-Za-z_][A-Za-z0-9_]*)["\']'
    r"|os\.environ\[['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\]"
)
WRITE_RE = re.compile(
    r'os\.environ\[["\']([A-Za-z_][A-Za-z0-9_]*)["\']'
    r"\]\s*(?:=|\+=|-=|\*=|/=)"
)


class EnvNameConventionTests(unittest.TestCase):
    def test_reads_are_jev_or_allowlisted(self) -> None:
        offenders: dict[str, list[str]] = {}
        for script in sorted(SCRIPTS.glob("*.py")):
            if script.stem.startswith("__"):
                continue
            src = script.read_text(encoding="utf-8", errors="replace")
            names = {
                m.group(1) or m.group(2) for m in READ_RE.finditer(src)
            }
            bad = sorted(
                n
                for n in names
                if not n.startswith(("JEV_", "TYPESAFE_"))
                and n not in ALLOWED
            )
            if bad:
                offenders[script.name] = bad
        self.assertEqual(offenders, {})

    def test_env_writes_are_jev_only(self) -> None:
        offenders: dict[str, list[str]] = {}
        for script in sorted(SCRIPTS.glob("*.py")):
            if script.stem.startswith("__"):
                continue
            src = script.read_text(encoding="utf-8", errors="replace")
            bad = sorted(
                n
                for n in set(WRITE_RE.findall(src))
                if not n.startswith(("JEV_", "TYPESAFE_"))
            )
            if bad:
                offenders[script.name] = bad
        self.assertEqual(offenders, {})


if __name__ == "__main__":
    unittest.main()

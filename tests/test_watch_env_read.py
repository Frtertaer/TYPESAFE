#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SKILL.md promises a JEV_<PREFIX>_WATCH_{MAX,SECS,QUIET} family —
pin each advertised prefix to the script that actually reads it."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

# prefix -> owning script, as documented in SKILL.md's env map
PREFIX_SCRIPT = {
    "HOOK": "inventory_hook.py", "PEER": "peer_fill.py",
    "CATALOG": "catalog_fill.py", "APPLY": "apply_fill.py",
    "INV": "inventory.py", "DECISIONS": "decisions.py",
    "COMPACT": "compact.py", "COMPARE": "compare.py",
    "DOCTOR": "doctor.py", "PLINT": "policy_lint.py",
    "SLINT": "skill_lint.py", "QLINT": "question_lint.py",
    "TLINT": "trigger_lint.py", "TRIGGER": "trigger_eval.py",
    "PING": "jev.py", "SMOKE": "smoke.py",
    "PROGRESS": "progress.py", "TRACE": "trace.py",
}
KNOBS = ("WATCH_MAX", "WATCH_SECS", "WATCH_QUIET")


class WatchEnvReadTests(unittest.TestCase):
    def test_every_documented_prefix_reads_all_knobs(self) -> None:
        for pfx, fname in sorted(PREFIX_SCRIPT.items()):
            src = (SCRIPTS / fname).read_text(encoding="utf-8")
            for knob in KNOBS:
                with self.subTest(prefix=pfx, knob=knob):
                    self.assertIn(
                        "JEV_%s_%s" % (pfx, knob), src,
                        "%s dropped JEV_%s_%s" % (fname, pfx, knob),
                    )


if __name__ == "__main__":
    unittest.main()

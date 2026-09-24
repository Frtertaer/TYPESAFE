#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Every JEV_<PFX>_WATCH_{MAX,SECS,QUIET} knob must be discoverable in
SKILL.md — either by exact name or a family wildcard JEV_<PFX>_WATCH_*.
A knob that exists in code but not in the doc is invisible."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_MD = (ROOT / "skills" / "jev-consult" / "SKILL.md").read_text(
    encoding="utf-8")

PREFIXES = [
    "HOOK", "PEER", "CATALOG", "APPLY", "INV", "DECISIONS", "COMPACT",
    "COMPARE", "DOCTOR", "PLINT", "SLINT", "QLINT", "TLINT", "TRIGGER",
    "PING", "SMOKE", "PROGRESS", "TRACE",
]
KNOBS = ["MAX", "SECS", "QUIET"]


class WatchKnobDocTests(unittest.TestCase):
    def test_every_watch_knob_is_documented(self) -> None:
        missing = []
        for pfx in PREFIXES:
            wildcard = "JEV_%s_WATCH_*" % pfx in SKILL_MD
            for knob in KNOBS:
                name = "JEV_%s_WATCH_%s" % (pfx, knob)
                global_wild = "JEV_*_WATCH_%s" % knob in SKILL_MD
                if name not in SKILL_MD and not wildcard and not global_wild:
                    missing.append(name)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()

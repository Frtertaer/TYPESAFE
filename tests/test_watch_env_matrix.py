"""Watch-env wiring matrix: every script with a --watch loop must pass the
same JEV_<PREFIX>_WATCH_MAX / _SECS / _QUIET triple to _watch, and the
_watch helpers must resolve flag > env > default."""
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _watch

EXPECTED_PREFIXES = {
    "apply_fill": "APPLY",
    "peer_fill": "PEER",
    "catalog_fill": "CATALOG",
    "question_lint": "QLINT",
    "skill_lint": "SLINT",
    "trigger_lint": "TLINT",
    "policy_lint": "PLINT",
    "trigger_eval": "TRIGGER",
    "inventory": "INV",
    "inventory_hook": "HOOK",
    "trace": "TRACE",
    "compact": "COMPACT",
    "compare": "COMPARE",
    "decisions": "DECISIONS",
    "doctor": "DOCTOR",
    "smoke": "SMOKE",
    "jev": "PING",
    "progress": "PROGRESS",
    # Standalone vendored script — wires JEV_SCAN_WATCH_* inline (no _watch).
    "skill_scanner": "SCAN",
}

ENV_RE = re.compile(r'"(JEV_([A-Z0-9]+)_WATCH_(MAX|SECS|QUIET))"')


class WatchWiringTest(unittest.TestCase):
    def test_each_watch_script_uses_one_complete_prefix(self) -> None:
        for path in sorted(SCRIPTS.glob("*.py")):
            if path.name == "_watch.py":
                continue
            src = path.read_text(encoding="utf-8")
            found = ENV_RE.findall(src)
            if not found:
                continue  # no watch loop
            stem = path.stem
            self.assertIn(stem, EXPECTED_PREFIXES, "unlisted watch script: %s" % stem)
            prefixes = {p for _full, p, _kind in found}
            kinds = {k for _full, _p, k in found}
            self.assertEqual(
                prefixes,
                {EXPECTED_PREFIXES[stem]},
                "%s wires mixed env prefixes: %s" % (stem, prefixes),
            )
            self.assertEqual(
                kinds,
                {"MAX", "SECS", "QUIET"},
                "%s missing watch env kinds: %s" % (stem, kinds),
            )

    def test_every_listed_prefix_exists(self) -> None:
        for stem in EXPECTED_PREFIXES:
            self.assertTrue(
                (SCRIPTS / (stem + ".py")).is_file(), "missing script %s" % stem
            )


class CapResolutionTest(unittest.TestCase):
    def test_flag_beats_env_beats_default(self) -> None:
        with patch.dict(os.environ, {"JEV_T_WATCH_MAX": "5"}, clear=False):
            self.assertEqual(_watch.cap("JEV_T_WATCH_MAX", 2), 2)  # flag wins
            self.assertEqual(_watch.cap("JEV_T_WATCH_MAX"), 5)  # env next
        self.assertEqual(_watch.cap("JEV_T_WATCH_MAX"), 0)  # default uncapped

    def test_bad_env_warns_and_uncaps(self) -> None:
        with patch.dict(os.environ, {"JEV_T_WATCH_MAX": "nope"}, clear=False):
            self.assertEqual(_watch.cap("JEV_T_WATCH_MAX"), 0)


class DeadlineResolutionTest(unittest.TestCase):
    def test_flag_beats_env(self) -> None:
        with patch.dict(os.environ, {"JEV_T_WATCH_SECS": "9"}, clear=False):
            flag_dead = _watch.deadline("JEV_T_WATCH_SECS", 1.0)
            env_dead = _watch.deadline("JEV_T_WATCH_SECS")
        self.assertLess(flag_dead, env_dead)
        self.assertGreater(flag_dead, 0)

    def test_zero_env_no_deadline(self) -> None:
        with patch.dict(os.environ, {"JEV_T_WATCH_SECS": "0"}, clear=False):
            self.assertEqual(_watch.deadline("JEV_T_WATCH_SECS"), 0.0)


class QuietResolutionTest(unittest.TestCase):
    def test_flag_beats_env(self) -> None:
        with patch.dict(os.environ, {"JEV_T_WATCH_QUIET": "0"}, clear=False):
            self.assertTrue(_watch.quiet("JEV_T_WATCH_QUIET", True))
        for truthy in ("1", "true", "yes", "on"):
            with patch.dict(os.environ, {"JEV_T_WATCH_QUIET": truthy}, clear=False):
                self.assertTrue(_watch.quiet("JEV_T_WATCH_QUIET", False), truthy)
        with patch.dict(os.environ, {"JEV_T_WATCH_QUIET": "no"}, clear=False):
            self.assertFalse(_watch.quiet("JEV_T_WATCH_QUIET", False))


if __name__ == "__main__":
    unittest.main()

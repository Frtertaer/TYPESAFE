"""Every script with --watch reads its OWN JEV_<X>_WATCH_* envs.

A copy-paste bug (script A reading script B's JEV_*_WATCH_MAX) would make
the documented env knobs silently useless — this guards the wiring.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

EXPECTED = {
    "smoke": "JEV_SMOKE",
    "compact": "JEV_COMPACT",
    "compare": "JEV_COMPARE",
    "apply_fill": "JEV_APPLY",
    "peer_fill": "JEV_PEER",
    "catalog_fill": "JEV_CATALOG",
    "trace": "JEV_TRACE",
    "trigger_eval": "JEV_TRIGGER",
    "question_lint": "JEV_QLINT",
    "policy_lint": "JEV_PLINT",
    "skill_lint": "JEV_SLINT",
    "trigger_lint": "JEV_TLINT",
    "inventory_hook": "JEV_HOOK",
    "inventory": "JEV_INV",
    "decisions": "JEV_DECISIONS",
    "doctor": "JEV_DOCTOR",
    "jev": "JEV_PING",
}

OTHER_ENVS = re.compile(r'"(JEV_[A-Z]+_WATCH_(?:MAX|SECS|QUIET))"')


class WatchEnvNameTests(unittest.TestCase):
    def test_each_watcher_uses_own_env_names(self) -> None:
        for stem, prefix in EXPECTED.items():
            src = (SCRIPTS / (stem + ".py")).read_text(encoding="utf-8")
            with self.subTest(script=stem):
                self.assertIn(
                    prefix + "_WATCH_MAX", src, "%s lost its %s knob" % (stem, prefix)
                )
                foreign = {
                    m
                    for m in OTHER_ENVS.findall(src)
                    if not m.startswith(prefix + "_")
                }
                self.assertEqual(
                    foreign, set(), "%s reads a foreign watch env: %s" % (stem, foreign)
                )

    def test_every_watcher_has_max_secs_quiet_triplet(self) -> None:
        for stem, prefix in EXPECTED.items():
            src = (SCRIPTS / (stem + ".py")).read_text(encoding="utf-8")
            with self.subTest(script=stem):
                for suffix in ("WATCH_MAX", "WATCH_SECS", "WATCH_QUIET"):
                    self.assertIn(
                        prefix + "_" + suffix,
                        src,
                        "%s missing %s_%s" % (stem, prefix, suffix),
                    )

    def test_no_new_watch_script_without_env(self) -> None:
        known = set(EXPECTED)
        for f in SCRIPTS.glob("*.py"):
            if f.stem.startswith("_") or f.stem in known:
                continue
            src = f.read_text(encoding="utf-8")
            with self.subTest(script=f.stem):
                self.assertNotIn(
                    '"--watch"',
                    src,
                    "%s gained --watch without being mapped in EXPECTED" % f.stem,
                )


if __name__ == "__main__":
    unittest.main()

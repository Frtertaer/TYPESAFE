"""Every pack script carries a working offline self-test.

--self-test (or a `self-test` subcommand on the argparse CLIs) is the
pack's built-in smoke check for harness-less environments; every script
must keep it green without network or Jev access, exit 0, and print an
ok marker. New scripts without one fail test_every_script_self_tests.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "skills" / "jev-consult" / "scripts"

# script -> self-test argv. Subcommand CLIs (jev, trace, progress) use
# `self-test`; flag-style scripts use --self-test.
SELF_TEST = {
    "_watch.py": ["--self-test"],
    "apply_fill.py": ["--self-test"],
    "catalog_fill.py": ["--self-test"],
    "compact.py": ["--self-test"],
    "compact_hook.py": ["--self-test"],
    "compare.py": ["--self-test"],
    "decisions.py": ["--self-test"],
    "doctor.py": ["--self-test"],
    "inventory.py": ["--self-test"],
    "inventory_hook.py": ["--self-test"],
    "jev.py": ["self-test"],
    "peer_fill.py": ["--self-test"],
    "policy_lint.py": ["--self-test"],
    "progress.py": ["self-test"],
    "question_lint.py": ["--self-test"],
    "skill_lint.py": ["--self-test"],
    "skill_scanner.py": ["--self-test"],
    "smoke.py": ["--self-test"],
    "trace.py": ["self-test"],
    "trigger_eval.py": ["--self-test"],
    "trigger_lint.py": ["--self-test"],
}

# progress_core is a library module without a CLI surface.
NON_SELF_TEST = {"progress_core.py"}


class SelfTestSweepTests(unittest.TestCase):
    def test_every_self_test_exits_zero_with_ok_marker(self) -> None:
        for name, argv in sorted(SELF_TEST.items()):
            with self.subTest(script=name):
                proc = subprocess.run(
                    [sys.executable, str(SCRIPTS_DIR / name), *argv],
                    capture_output=True,
                    text=True,
                    timeout=90,
                    stdin=subprocess.DEVNULL,
                )
                self.assertEqual(
                    proc.returncode,
                    0,
                    "%s rc=%d: %s%s"
                    % (name, proc.returncode, proc.stdout[:200], proc.stderr[:200]),
                )
                self.assertNotIn("Traceback", proc.stderr, name)
                out = proc.stdout
                ok = '"self_test": "ok"' in out or "self-test: ok" in out
                self.assertTrue(
                    ok, "%s self-test lacks ok marker: %s" % (name, out[:200])
                )

    def test_every_script_self_tests(self) -> None:
        present = {
            f.name for f in SCRIPTS_DIR.glob("*.py")
        } - NON_SELF_TEST
        self.assertEqual(
            present - set(SELF_TEST),
            set(),
            "scripts without a pinned self-test: %s"
            % sorted(present - set(SELF_TEST)),
        )

    def test_self_test_json_variants_parse(self) -> None:
        """The subcommand self-tests emit JSON; parse and check the marker."""
        for name in ("trace.py", "progress.py"):
            with self.subTest(script=name):
                proc = subprocess.run(
                    [sys.executable, str(SCRIPTS_DIR / name), "self-test"],
                    capture_output=True,
                    text=True,
                    timeout=90,
                    stdin=subprocess.DEVNULL,
                )
                payload = json.loads(proc.stdout)
                self.assertEqual(payload.get("self_test"), "ok", name)


if __name__ == "__main__":
    unittest.main()

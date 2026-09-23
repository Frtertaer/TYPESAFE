from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "skills" / "jev-consult" / "scripts"

# Vendored or helper modules without a self-testable CLI surface.
EXCLUDED = {"_watch.py", "skill_scanner.py"}

# Scripts whose self-test is a subcommand rather than the --self-test flag.
SUBCOMMAND = {"jev.py": "self-test", "trace.py": "self-test"}


class SelfTestParityTests(unittest.TestCase):
    def test_every_pack_script_answers_self_test(self) -> None:
        scripts = sorted(
            p.name for p in SCRIPTS_DIR.glob("*.py") if p.name not in EXCLUDED
        )
        self.assertGreater(len(scripts), 10)
        for name in scripts:
            with self.subTest(script=name):
                argv = [sys.executable, str(SCRIPTS_DIR / name)]
                argv.append(SUBCOMMAND.get(name, "--self-test"))
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                self.assertEqual(
                    proc.returncode,
                    0,
                    "%s self-test rc=%d: %s" % (name, proc.returncode, proc.stdout + proc.stderr),
                )
                self.assertTrue(
                    "self-test: ok" in proc.stdout
                    or '"self_test": "ok"' in proc.stdout,
                    "%s self-test output: %s" % (name, proc.stdout[:200]),
                )


if __name__ == "__main__":
    unittest.main()

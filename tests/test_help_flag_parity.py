"""--help must advertise every flag a script's argv actually accepts.

Argparse scripts can't drift (add_argument feeds the help text), but the
manual-argv scripts keep a hand-written usage block — a new flag that
forgets to add its line is invisible to users. This greps each manual
script's `"--flag"` literals and requires each to appear in --help.
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "skills" / "jev-consult" / "scripts"

MANUAL = (
    "compact_hook.py",
    "inventory_hook.py",
    "policy_lint.py",
    "question_lint.py",
    "skill_lint.py",
    "trigger_lint.py",
)


class HelpFlagParityTests(unittest.TestCase):
    def test_manual_scripts_help_lists_every_flag(self) -> None:
        for name in MANUAL:
            with self.subTest(script=name):
                path = SCRIPTS_DIR / name
                src = path.read_text(encoding="utf-8")
                flags = set(re.findall(r'"(--[a-z][a-z-]+)"', src))
                proc = subprocess.run(
                    [sys.executable, str(path), "--help"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr[:200])
                missing = sorted(f for f in flags if f not in proc.stdout)
                self.assertEqual(
                    missing, [], "%s --help omits %s" % (name, missing)
                )


if __name__ == "__main__":
    unittest.main()

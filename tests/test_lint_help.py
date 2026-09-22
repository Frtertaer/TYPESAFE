"""Every scripts/*.py supports --help / -h: exit 0 and print a usage line.

Guard against the manual-argv linters regressing back to treating --help
as an input path (P000/T001/S001 style "cannot parse --help" errors).
"""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"


def scripts() -> list[Path]:
    return sorted(p for p in SCRIPTS.glob("*.py") if not p.name.startswith("_"))


class HelpFlagTests(unittest.TestCase):
    def test_help_exits_zero_and_prints_usage(self) -> None:
        for script in scripts():
            proc = subprocess.run(
                [sys.executable, str(script), "--help"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            with self.subTest(script=script.name):
                self.assertEqual(proc.returncode, 0, proc.stderr[:300])
                self.assertIn(
                    "usage",
                    proc.stdout.lower(),
                    "%s printed no usage on --help" % script.name,
                )

    def test_h_short_flag_where_supported(self) -> None:
        # Manual-argv linters explicitly accept -h too.
        for name in ("policy_lint.py", "question_lint.py", "trigger_lint.py", "skill_lint.py"):
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / name), "-h"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            with self.subTest(script=name):
                self.assertEqual(proc.returncode, 0, proc.stderr[:300])
                self.assertIn("usage", proc.stdout.lower())

    def test_help_does_not_lint_anything(self) -> None:
        # --help must short-circuit before file parsing: no findings output.
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "policy_lint.py"), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("P000", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()

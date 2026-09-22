"""Unknown flags never produce a traceback, and hooks stay fail-open.

argparse scripts reject unknown flags with rc 2; the manual-argv linters
treat them as input paths and error out (rc 1/2); the stdin hooks ignore
them and emit `{}` — both behaviors are correct, but NONE may crash.
"""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
FLAG = "--definitely-not-a-real-flag"

# stdin hooks: any argv is fine, they still emit the empty payload.
HOOKS = ("inventory_hook.py", "compact_hook.py")


def scripts() -> list[Path]:
    return sorted(p for p in SCRIPTS.glob("*.py") if not p.name.startswith("_"))


class UnknownFlagTests(unittest.TestCase):
    def test_no_traceback_on_unknown_flag(self) -> None:
        for script in scripts():
            proc = subprocess.run(
                [sys.executable, str(script), FLAG],
                input="",
                capture_output=True,
                text=True,
                timeout=30,
            )
            with self.subTest(script=script.name):
                self.assertNotIn(
                    "Traceback",
                    proc.stderr + proc.stdout,
                    "%s crashed on %s" % (script.name, FLAG),
                )

    def test_hooks_still_emit_empty_payload(self) -> None:
        for name in HOOKS:
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / name), FLAG],
                input="",
                capture_output=True,
                text=True,
                timeout=30,
            )
            with self.subTest(script=name):
                self.assertEqual(proc.returncode, 0, proc.stderr[:200])
                self.assertEqual(proc.stdout.strip(), "{}")

    def test_non_hook_scripts_reject_unknown_flag(self) -> None:
        for script in scripts():
            if script.name in HOOKS:
                continue
            proc = subprocess.run(
                [sys.executable, str(script), FLAG],
                input="",
                capture_output=True,
                text=True,
                timeout=30,
            )
            with self.subTest(script=script.name):
                self.assertNotEqual(
                    proc.returncode, 0, "%s accepted %s" % (script.name, FLAG)
                )


if __name__ == "__main__":
    unittest.main()

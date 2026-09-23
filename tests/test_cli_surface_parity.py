"""CLI surface parity: --help and --version behave uniformly.

--help must exit 0 and print usage text on every CLI script (including
the vendored scanner). --version must exit 0 and print the
'jev-consult (policy vN)' tag on every non-vendored script — the tag is
how hooks/operators confirm which pack generation is installed.
"""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "skills" / "jev-consult" / "scripts"

VERSION_TAG = "jev-consult (policy v"

# Modules without a CLI surface.
NON_CLI = {"_watch.py", "progress_core.py"}
# Vendored: argparse-only, no --version flag.
NO_VERSION = {"skill_scanner.py"}


def _cli_scripts() -> set[str]:
    return {
        f.name for f in SCRIPTS_DIR.glob("*.py") if not f.name.startswith("_")
    } - NON_CLI


def _run(name: str, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / name), *argv],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


class CliSurfaceParityTests(unittest.TestCase):
    def test_help_exits_0_with_usage(self) -> None:
        for name in sorted(_cli_scripts()):
            with self.subTest(script=name):
                proc = _run(name, "--help")
                self.assertEqual(
                    proc.returncode,
                    0,
                    "%s --help rc=%d: %s" % (name, proc.returncode, proc.stderr[:200]),
                )
                self.assertIn("usage", proc.stdout.lower(), name)
                self.assertNotIn("Traceback", proc.stderr, name)

    def test_version_prints_pack_tag(self) -> None:
        for name in sorted(_cli_scripts() - NO_VERSION):
            with self.subTest(script=name):
                proc = _run(name, "--version")
                self.assertEqual(
                    proc.returncode,
                    0,
                    "%s --version rc=%d: %s" % (name, proc.returncode, proc.stderr[:200]),
                )
                self.assertIn(VERSION_TAG, proc.stdout, name)


if __name__ == "__main__":
    unittest.main()

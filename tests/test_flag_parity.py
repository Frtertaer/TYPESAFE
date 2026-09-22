"""Every non-vendored pack script honors --version and --help (SKILL.md contract)."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "jev-consult" / "scripts"


def _cli_scripts() -> list[Path]:
    out = []
    for path in sorted(SCRIPTS.glob("*.py")):
        if path.name.startswith("_"):
            continue
        if "[vendored]" in path.read_text(encoding="utf-8")[:600]:
            continue
        out.append(path)
    return out


class FlagParityTest(unittest.TestCase):
    def test_every_script_supports_version_and_help(self) -> None:
        scripts = _cli_scripts()
        self.assertGreater(len(scripts), 10)
        for path in scripts:
            with self.subTest(script=path.name):
                ver = subprocess.run(
                    [sys.executable, str(path), "--version"],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(ver.returncode, 0, ver.stderr)
                self.assertIn("jev-consult", ver.stdout)
                help_run = subprocess.run(
                    [sys.executable, str(path), "--help"],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(help_run.returncode, 0, help_run.stderr)
                self.assertIn("sage:", help_run.stdout.lower() + help_run.stderr.lower())


if __name__ == "__main__":
    unittest.main()

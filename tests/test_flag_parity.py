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

    def test_fill_scripts_share_core_flags(self) -> None:
        # apply_fill / peer_fill / catalog_fill are three skins over the same
        # fill flow; drift in their core flag set is a bug, extras are pinned.
        import re

        core = {
            "--task", "--harness", "--home", "--hermes-home", "--cwd",
            "--pick", "--from-miss", "--dry-run", "--ask-file", "--json",
            "--jq", "--watch", "--max-ticks", "--watch-max", "--quiet",
            "--fail-fast", "--out", "--verdict", "--help",
        }
        extras = {
            "apply_fill.py": {"--status", "--list", "--show"},
            "peer_fill.py": {"--status", "--list", "--show"},
            "catalog_fill.py": {"--clear", "--list", "--show"},
        }
        for name, allowed in extras.items():
            path = SCRIPTS / name
            with self.subTest(script=name):
                help_run = subprocess.run(
                    [sys.executable, str(path), "--help"],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(help_run.returncode, 0, help_run.stderr)
                found = set(re.findall(r"--[a-z][a-z-]*", help_run.stdout))
                found.discard("--version")
                missing = core - found
                self.assertFalse(missing, "%s missing core flags %s" % (name, sorted(missing)))
                unexpected = found - core - allowed
                self.assertFalse(
                    unexpected, "%s has unpinned extra flags %s" % (name, sorted(unexpected))
                )


if __name__ == "__main__":
    unittest.main()

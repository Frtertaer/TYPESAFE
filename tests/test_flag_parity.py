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
            "--fail-fast", "--out", "--verdict", "--schema", "--help",
        }
        extras = {
            "apply_fill.py": {"--status", "--list", "--show", "--self-test"},
            "peer_fill.py": {"--status", "--list", "--show", "--self-test"},
            "catalog_fill.py": {"--clear", "--status", "--list", "--show", "--self-test"},
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

    WATCH_CORE = {
        "--watch", "--max-ticks", "--watch-max", "--quiet",
        "--fail-fast", "--out", "--verdict", "--jq",
    }
    # Every help screen that advertises --watch, keyed by script. Flat-CLI
    # scripts map to one bare --help; subcommand CLIs list each watchable
    # subcommand so the flag set is pinned where the flag actually lives.
    WATCH_HELP = {
        "compact.py": [["--help"]],
        "decisions.py": [["--help"]],
        "inventory.py": [["--help"]],
        "compare.py": [["--help"]],
        "doctor.py": [["--help"]],
        "question_lint.py": [["--help"]],
        "skill_lint.py": [["--help"]],
        "policy_lint.py": [["--help"]],
        "trigger_lint.py": [["--help"]],
        "trigger_eval.py": [["--help"]],
        "inventory_hook.py": [["--help"]],
        "smoke.py": [["--help"]],
        "jev.py": [["ping", "--help"]],
        "trace.py": [
            ["state", "--help"], ["notes", "--help"],
            ["history", "--help"], ["stats", "--help"],
        ],
        "apply_fill.py": [["--help"]],
        "peer_fill.py": [["--help"]],
        "catalog_fill.py": [["--help"]],
    }

    def test_watch_helps_advertise_full_flag_set(self) -> None:
        import re

        for name, argv_variants in self.WATCH_HELP.items():
            for argv in argv_variants:
                label = "%s %s" % (name, " ".join(argv).strip())
                with self.subTest(help=label):
                    run = subprocess.run(
                        [sys.executable, str(SCRIPTS / name), *argv],
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(run.returncode, 0, run.stderr)
                    found = set(re.findall(r"--[a-z][a-z-]*", run.stdout))
                    missing = self.WATCH_CORE - found
                    self.assertFalse(
                        missing, "%s missing watch flags %s" % (label, sorted(missing))
                    )

    def test_every_watching_script_is_pinned(self) -> None:
        """A script whose argv handles --watch must have a WATCH_HELP entry."""
        import re

        for path in _cli_scripts():
            src = path.read_text(encoding="utf-8")
            handles_watch = '"--watch" in argv' in src or bool(
                re.search(r'^\s*"--watch",\s*$', src, re.M)
            )
            with self.subTest(script=path.name):
                self.assertEqual(
                    handles_watch,
                    path.name in self.WATCH_HELP,
                    "%s watch handling is unpinned (add/remove a WATCH_HELP entry)"
                    % path.name,
                )


if __name__ == "__main__":
    unittest.main()

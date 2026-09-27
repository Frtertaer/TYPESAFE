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


class VersionSweepTests(unittest.TestCase):
    """Every script answers --version rc 0 with the pack policy version."""

    def test_every_script_prints_policy_version(self) -> None:
        import json as _json

        policy_v = str(
            _json.loads(
                (SCRIPTS.parent / "policy.json").read_text(encoding="utf-8")
            ).get("version", "?")
        )
        expected = "jev-consult (policy v%s)" % policy_v
        for script in scripts():
            proc = subprocess.run(
                [sys.executable, str(script), "--version"],
                input="",
                capture_output=True,
                text=True,
                timeout=30,
            )
            with self.subTest(script=script.name):
                if script.name == "skill_scanner.py":
                    # vendored upstream tool — its own --version semantics
                    continue
                self.assertEqual(proc.returncode, 0, proc.stderr[:200])
                self.assertEqual(proc.stdout.strip(), expected)


class EmptyStdinTests(unittest.TestCase):
    """EOF on stdin, bare argv: no script may traceback; hooks emit `{}`."""

    def test_no_traceback_on_empty_stdin(self) -> None:
        for script in scripts():
            proc = subprocess.run(
                [sys.executable, str(script)],
                input="",
                capture_output=True,
                text=True,
                # smoke.py runs the whole pack; its bound scales with the
                # suite, not with a one-shot script
                timeout=120 if script.name == "smoke.py" else 60,
            )
            with self.subTest(script=script.name):
                self.assertNotIn(
                    "Traceback",
                    proc.stderr + proc.stdout,
                    "%s crashed on empty stdin" % script.name,
                )

    def test_hooks_emit_empty_payload_on_eof(self) -> None:
        for name in HOOKS:
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / name)],
                input="",
                capture_output=True,
                text=True,
                timeout=30,
            )
            with self.subTest(script=name):
                self.assertEqual(proc.returncode, 0, proc.stderr[:200])
                self.assertEqual(proc.stdout.strip(), "{}")


if __name__ == "__main__":
    unittest.main()

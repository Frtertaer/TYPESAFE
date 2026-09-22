"""TYPESAFE_API_KEY must never reach stdout/stderr — canary-based guard.

Every script is invoked with a sentinel key value; its bytes may never appear
in any output channel, on any code path exercised here (help, env report,
dry-run, empty stdin, malformed input).
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
CANARY = "CANARY_KEY_9f8e7d6c5b4a"


def run(script: str, argv: list, stdin: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TYPESAFE_API_KEY"] = CANARY
    env["JEV_CONSULT_LOG"] = "0"
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *argv],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )


class ApiKeyGuardTests(unittest.TestCase):
    def _assert_no_canary(self, proc: subprocess.CompletedProcess, tag: str) -> None:
        self.assertNotIn(CANARY, proc.stdout, "%s stdout leaked key" % tag)
        self.assertNotIn(CANARY, proc.stderr, "%s stderr leaked key" % tag)

    def test_hook_empty_stdin_no_leak(self) -> None:
        self._assert_no_canary(run("inventory_hook.py", []), "hook-empty")

    def test_hook_garbage_stdin_no_leak(self) -> None:
        self._assert_no_canary(run("inventory_hook.py", [], stdin="junk"), "hook-junk")

    def test_hook_env_report_no_leak(self) -> None:
        self._assert_no_canary(run("inventory_hook.py", ["--env"]), "hook-env")

    def test_hook_verbose_no_leak(self) -> None:
        self._assert_no_canary(
            run("inventory_hook.py", ["--verbose"], stdin="{}"), "hook-verbose"
        )

    def test_inventory_no_leak(self) -> None:
        self._assert_no_canary(run("inventory.py", ["--task", "jwt"]), "inventory")

    def test_doctor_no_leak(self) -> None:
        self._assert_no_canary(run("doctor.py", ["--json"]), "doctor")

    def test_smoke_no_leak(self) -> None:
        self._assert_no_canary(run("smoke.py", ["--dry-run"]), "smoke")

    def test_check_key_no_leak(self) -> None:
        install = ROOT / "scripts" / "install.py"
        env = dict(os.environ)
        env["TYPESAFE_API_KEY"] = CANARY
        proc = subprocess.run(
            [sys.executable, str(install), "--check-key"],
            capture_output=True,
            text=True,
            env=env,
        )
        self._assert_no_canary(proc, "check-key")


if __name__ == "__main__":
    unittest.main(verbosity=2)

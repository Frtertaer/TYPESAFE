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


def run(
    script: str, argv: list, stdin: str = "", extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TYPESAFE_API_KEY"] = CANARY
    env["JEV_CONSULT_LOG"] = "0"
    if extra_env:
        env.update(extra_env)
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



    def test_hook_debug_stderr_no_leak(self) -> None:
        proc = run(
            "inventory_hook.py",
            ["--debug"],
            stdin='{"hook_event_name": "UserPromptSubmit", "prompt": "jwt"}',
            extra_env={"JEV_HOOK_DEBUG": "1"},
        )
        self._assert_no_canary(proc, "hook-debug")

    def test_hook_bad_env_values_warn_no_leak(self) -> None:
        proc = run(
            "inventory_hook.py",
            [],
            stdin="{}",
            extra_env={
                "JEV_HOOK_TIMEOUT": "bogus",
                "JEV_HOOK_COOLDOWN": "bogus",
                "JEV_HOOK_MAX_PROMPT": "bogus",
            },
        )
        self._assert_no_canary(proc, "hook-badenv")

    def test_hook_jq_stderr_no_leak(self) -> None:
        proc = run("inventory_hook.py", ["--jq"], stdin="{}")
        self._assert_no_canary(proc, "hook-jq")

    def test_compact_missing_file_no_leak(self) -> None:
        proc = run("compact.py", ["does-not-exist-transcript.json"])
        self._assert_no_canary(proc, "compact-missing")

    def test_decisions_missing_log_no_leak(self) -> None:
        proc = run("decisions.py", ["--log", "does-not-exist.jsonl"])
        self._assert_no_canary(proc, "decisions-missing")

if __name__ == "__main__":
    unittest.main(verbosity=2)

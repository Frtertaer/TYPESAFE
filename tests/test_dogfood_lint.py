"""Dogfood pins: the pack's own artifacts must pass its own linters.

skill_lint on SKILL.md, policy_lint on policy.json, and trigger_lint on
the trigger-cases fixture must all run clean — rc 0, no error/warning
findings (info-level findings are allowed). A regression here means the
pack ships an artifact its own rules reject.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
SKILL_DIR = ROOT / "skills" / "jev-consult"
TRIGGER_CASES = ROOT / "tests" / "fixtures" / "jev-consult.trigger-cases.json"


def _lint(script: str, target: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), str(target), "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


class DogfoodLintTests(unittest.TestCase):
    def _assert_clean(self, proc: subprocess.CompletedProcess, label: str) -> None:
        self.assertNotIn("Traceback", proc.stderr, label)
        # --json prints a findings list (or object) regardless of rc.
        payload = json.loads(proc.stdout)
        findings = payload if isinstance(payload, list) else payload.get("findings", [])
        bad = [
            f
            for f in findings
            if isinstance(f, dict) and f.get("severity") in ("error", "warn", "warning")
        ]
        self.assertEqual(bad, [], "%s findings: %s" % (label, bad))

    def test_skill_md_passes_skill_lint(self) -> None:
        self._assert_clean(_lint("skill_lint.py", SKILL_DIR / "SKILL.md"), "skill_lint")

    def test_policy_json_passes_policy_lint(self) -> None:
        self._assert_clean(
            _lint("policy_lint.py", SKILL_DIR / "policy.json"), "policy_lint"
        )

    def test_trigger_cases_pass_trigger_lint(self) -> None:
        self._assert_clean(_lint("trigger_lint.py", TRIGGER_CASES), "trigger_lint")


if __name__ == "__main__":
    unittest.main()

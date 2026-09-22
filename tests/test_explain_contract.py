"""`--explain RULE` exits 2 on an unknown rule id for every lint tool.

Each lint prints `unknown rule 'X' (rules: ...)` so the user sees the
valid ids; the contract also guards the known-rule prefix per tool.
"""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

LINTS = {
    "policy_lint.py": "P",
    "question_lint.py": "J",
    "skill_lint.py": "S",
    "trigger_lint.py": "T",
}


class ExplainContractTests(unittest.TestCase):
    def test_unknown_rule_exits_2_with_rule_list(self) -> None:
        problems = []
        for script, prefix in sorted(LINTS.items()):
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / script), "--explain", "ZZZ"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 2 or "unknown rule" not in proc.stderr:
                problems.append(
                    "%s: rc=%d err=%r"
                    % (script, proc.returncode, proc.stderr[:80])
                )
                continue
            if "%s001" % prefix not in proc.stderr:
                problems.append(
                    "%s: rule list missing %s-prefixed ids" % (script, prefix)
                )
        self.assertEqual(problems, [])

    def test_known_rule_exits_0(self) -> None:
        problems = []
        for script, prefix in sorted(LINTS.items()):
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / script),
                    "--explain",
                    "%s001" % prefix,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                problems.append(
                    "%s: rc=%d out=%r"
                    % (script, proc.returncode, proc.stdout[:80])
                )
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

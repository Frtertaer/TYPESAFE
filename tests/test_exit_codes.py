"""Documented exit-code contracts hold: `rc 2 on unknown` for --explain
RULE and for the `--env --jq` dotted-path lookup."""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

# scripts accepting --explain RULE (docs: rc 2 on unknown rule)
EXPLAIN = [
    "compact.py",
    "inventory.py",
    "policy_lint.py",
    "question_lint.py",
    "skill_lint.py",
    "trigger_lint.py",
]


def run(name: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / name), *args],
        input="",
        capture_output=True,
        text=True,
        timeout=30,
    )


class ExplainRcTests(unittest.TestCase):
    def test_unknown_explain_rule_is_rc2(self) -> None:
        for name in EXPLAIN:
            with self.subTest(script=name):
                proc = run(name, "--explain", "ZZZ-NOPE")
                self.assertEqual(
                    proc.returncode,
                    2,
                    "%s rc=%s err=%s" % (name, proc.returncode, proc.stderr[:200]),
                )
                self.assertNotIn("Traceback", proc.stderr)

    def test_known_explain_rule_is_rc0(self) -> None:
        # each linter has a rule prefix; first known rule per script
        known = {
            "policy_lint.py": "P001",
            "question_lint.py": "J001",
            "skill_lint.py": "S001",
            "trigger_lint.py": "T001",
        }
        for name, rule in known.items():
            with self.subTest(script=name, rule=rule):
                proc = run(name, "--explain", rule)
                self.assertEqual(
                    proc.returncode,
                    0,
                    "%s --explain %s rc=%s err=%s"
                    % (name, rule, proc.returncode, proc.stderr[:200]),
                )
                self.assertTrue(proc.stdout.strip())


class EnvJqRcTests(unittest.TestCase):
    def test_trigger_eval_env_bad_jq_key_is_rc2(self) -> None:
        proc = run("trigger_eval.py", "--env", "--jq", "definitely_not_a_key")
        self.assertEqual(proc.returncode, 2, proc.stderr[:200] + proc.stdout[:200])
        self.assertNotIn("Traceback", proc.stderr)

    def test_trigger_eval_env_known_jq_key_is_rc0(self) -> None:
        proc = run("trigger_eval.py", "--env", "--jq", "margin")
        self.assertEqual(proc.returncode, 0, proc.stderr[:200])
        self.assertTrue(proc.stdout.strip())


if __name__ == "__main__":
    unittest.main()

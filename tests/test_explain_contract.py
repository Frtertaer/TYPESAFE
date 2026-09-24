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

    def test_explain_dash_reads_rule_from_stdin(self) -> None:
        problems = []
        for script, prefix in sorted(LINTS.items()):
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / script), "--explain", "-"],
                input="%s001\n" % prefix,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0 or not proc.stdout.startswith("%s001:" % prefix):
                problems.append(
                    "%s: rc=%d out=%r"
                    % (script, proc.returncode, proc.stdout[:80])
                )
        self.assertEqual(problems, [])

    def test_md_flag_renders_findings_table(self) -> None:
        cases = {
            "policy_lint.py": ('-', '{"version": 1}'),
            "question_lint.py": (
                '-',
                '{"questions": {"q": {"type": "noul", "instructions": '
                '"Is it done and does it pass?", "criteria": {"true": "y", "false": "n"}}}}',
            ),
            "skill_lint.py": ('-', '# no frontmatter\n'),
            "trigger_lint.py": (
                '-',
                '{"cases": [{"id": "dup", "should_trigger": true}, {"id": "dup"}]}',
            ),
        }
        problems = []
        for script, (arg, stdin) in sorted(cases.items()):
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / script), arg, "--md"],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode not in (0, 1):
                problems.append(
                    "%s: rc=%d err=%r" % (script, proc.returncode, proc.stderr[:80])
                )
                continue
            if not proc.stdout.startswith("| severity |") or "| " + LINTS[script] not in proc.stdout:
                problems.append(
                    "%s: out=%r" % (script, proc.stdout[:80])
                )
        self.assertEqual(problems, [])

    def test_rules_lists_every_rule(self) -> None:
        import json as _json

        problems = []
        for script, prefix in sorted(LINTS.items()):
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / script), "--rules"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                problems.append("%s: rc=%d" % (script, proc.returncode))
                continue
            lines = [
                l for l in proc.stdout.splitlines() if l.startswith(prefix)
            ]
            if not lines or any(": " not in l for l in lines):
                problems.append("%s: out=%r" % (script, proc.stdout[:80]))
                continue
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / script), "--rules", "--json"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            payload = _json.loads(proc.stdout)
            if not isinstance(payload, list) or not all(
                r.get("rule", "").startswith(prefix) and r.get("description")
                for r in payload
            ):
                problems.append(
                    "%s json: %r" % (script, proc.stdout[:80])
                )
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

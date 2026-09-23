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


class LintRuleExplainParityTests(unittest.TestCase):
    """Every rule id a lint emits must have an --explain entry in RULES."""

    LINTS = {
        "question_lint.py": "J",
        "policy_lint.py": "P",
        "skill_lint.py": "S",
        "trigger_lint.py": "T",
    }

    def test_emitted_rule_ids_are_all_explainable(self) -> None:
        import re
        import importlib.util

        for name, prefix in self.LINTS.items():
            path = SCRIPTS / name
            src = path.read_text(encoding="utf-8")
            # drop the RULES dict itself so only emission sites remain
            body = re.sub(r"RULES = \{.*?\n\}", "", src, flags=re.S)
            emitted = set(re.findall(r'"(%s\d{3})"' % prefix, body))
            spec = importlib.util.spec_from_file_location(name[:-3], path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            rules = set(mod.RULES)
            self.assertTrue(emitted, "%s emits no rule ids?" % name)
            missing = emitted - rules
            self.assertEqual(
                missing, set(), "%s emits rules with no --explain: %s" % (name, missing)
            )
            for rule in emitted:
                proc = run(name, "--explain", rule)
                self.assertEqual(proc.returncode, 0, "%s --explain %s" % (name, rule))


class SeverityEnvSweepTests(unittest.TestCase):
    """JEV_*_SEVERITY presets the --severity floor on every lint."""

    def _fixture(self, tmp: Path, name: str) -> Path:
        import json as _json

        if name == "question_lint.py":
            f = tmp / "req.json"
            f.write_text(
                _json.dumps(
                    {
                        "questions": {
                            "a": {
                                "type": "noul",
                                "instructions": "Should the coder not proceed with this task?",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            return f
        if name == "policy_lint.py":
            import shutil

            src = SCRIPTS.parent / "policy.json"
            f = tmp / "policy.json"
            shutil.copy(src, f)
            data = _json.loads(f.read_text(encoding="utf-8"))
            data["bogus_key"] = 1  # P011 warn only
            f.write_text(_json.dumps(data), encoding="utf-8")
            return f
        if name == "skill_lint.py":
            d = tmp / "x-skill"
            d.mkdir()
            f = d / "SKILL.md"
            f.write_text("---\nname: x-skill\n---\nbody\n", encoding="utf-8")
            return f  # S004 warn: missing description
        f = tmp / "cases.json"
        f.write_text(
            _json.dumps(
                {
                    "cases": [
                        {
                            "id": "BadId",  # T005 warn: not pos-/neg- prefixed
                            "prompt": "use the jev consult skill now please",
                            "should_trigger": True,
                            "covers": ["approach"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return f

    CASES = (
        ("question_lint.py", "JEV_QLINT_SEVERITY", "J001"),
        ("policy_lint.py", "JEV_PLINT_SEVERITY", "P011"),
        ("skill_lint.py", "JEV_SLINT_SEVERITY", "S004"),
        ("trigger_lint.py", "JEV_TLINT_SEVERITY", "T005"),
    )

    def test_env_floor_suppresses_warn_findings(self) -> None:
        import json
        import os
        import tempfile

        for name, env_name, rule in self.CASES:
            with self.subTest(lint=name):
                with tempfile.TemporaryDirectory() as tmp:
                    fixture = self._fixture(Path(tmp), name)
                    proc = subprocess.run(
                        [sys.executable, str(SCRIPTS / name), str(fixture), "--json"],
                        capture_output=True, text=True, timeout=30,
                    )
                    data = json.loads(proc.stdout)
                    rows = data["findings"] if isinstance(data, dict) else data
                    self.assertIn(rule, [r["rule"] for r in rows], proc.stdout[:300])
                    env = dict(os.environ, **{env_name: "error"})
                    proc = subprocess.run(
                        [sys.executable, str(SCRIPTS / name), str(fixture), "--json"],
                        capture_output=True, text=True, timeout=30, env=env,
                    )
                    data = json.loads(proc.stdout)
                    rows = data["findings"] if isinstance(data, dict) else data
                    self.assertNotIn(rule, [r["rule"] for r in rows], proc.stdout[:300])


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

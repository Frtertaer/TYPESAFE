"""Lint --fix must be idempotent: a second --fix on fixed output is a no-op."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
POLICY = ROOT / "skills" / "jev-consult" / "policy.json"
CASES = ROOT / "tests" / "fixtures" / "jev-consult.trigger-cases.json"


def run(script: str, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *argv],
        capture_output=True,
        text=True,
    )


class PolicyFixRoundtripTests(unittest.TestCase):
    def test_policy_fix_then_refixed_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            data = json.loads(POLICY.read_text(encoding="utf-8"))
            data["bogus_key"] = 1
            data.setdefault("escalate_if", {})["nope"] = 0.5
            path.write_text(json.dumps(data), encoding="utf-8")
            first = run("policy_lint.py", str(path), "--fix")
            self.assertEqual(first.returncode, 0, first.stderr)
            after_first = path.read_bytes()
            second = run("policy_lint.py", str(path), "--fix")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(path.read_bytes(), after_first)
            fixed = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("bogus_key", fixed)
            self.assertNotIn("nope", fixed["escalate_if"])


class QuestionFixRoundtripTests(unittest.TestCase):
    def test_question_fix_then_refixed_is_noop(self) -> None:
        request = {
            "questions": {
                "q1": {
                    "type": "choice",
                    "text": "pick one",
                    "choices": ["a", "b"],
                    "criteria": {"a": "", "b": ""},
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "req.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            first = run("question_lint.py", str(path), "--fix")
            self.assertEqual(first.returncode, 0, first.stderr)
            after_first = path.read_bytes()
            second = run("question_lint.py", str(path), "--fix")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(path.read_bytes(), after_first)


class TriggerFixRoundtripTests(unittest.TestCase):
    def test_trigger_fix_then_refixed_is_noop(self) -> None:
        data = json.loads(CASES.read_text(encoding="utf-8"))
        data.setdefault("cases", []).append(
            {"id": "Bad Case ID!!", "prompt": "x", "should_trigger": False}
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            first = run("trigger_lint.py", str(path), "--fix")
            self.assertEqual(first.returncode, 0, first.stderr)
            after_first = path.read_bytes()
            second = run("trigger_lint.py", str(path), "--fix")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(path.read_bytes(), after_first)


if __name__ == "__main__":
    unittest.main(verbosity=2)

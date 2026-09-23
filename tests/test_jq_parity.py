from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "skills" / "jev-consult" / "scripts"
SKILL_MD = ROOT / "skills" / "jev-consult" / "SKILL.md"
TRIGGER_CASES = ROOT / "tests" / "fixtures" / "jev-consult.trigger-cases.json"



def _argv(name: str, tmp: str) -> list[str]:
    """An offline invocation that reaches each script's --jq payload check."""
    base = [sys.executable, str(SCRIPTS_DIR / name)]
    request_path = Path(tmp) / "request.json"
    if not request_path.exists():
        with open(request_path, "w", encoding="utf-8") as fh:
            fh.write(
                '{"questions":{"q":{"type":"choice","criteria":["a","none"],"prompt":"pick one"}}}'
            )
    extra = {
        "inventory.py": ["--task", "x", "--home", tmp],
        "peer_fill.py": ["--status", "--cwd", tmp],
        "apply_fill.py": ["--status", "--cwd", tmp],
        "catalog_fill.py": ["--status", "--cwd", tmp],
        "compact.py": ["--fake", "--dir", tmp],
        "smoke.py": ["--only", "self_test"],
        "trace.py": ["stats", "--file", str(Path(tmp) / "missing.json")],
        "trigger_eval.py": ["--json"],
        "skill_lint.py": [str(SKILL_MD)],
        "question_lint.py": [str(request_path)],
        "trigger_lint.py": [str(TRIGGER_CASES)],
        "jev.py": ["decide", "-"],
    }.get(name, [])
    return base + extra + ["--jq", "nope"]


# Scripts with --jq that dig a payload object; unknown key must exit 2.
# decisions.py is excluded: its --jq is per-entry field extraction
# (values list), not a payload dig, so unknown keys legitimately print [].
PAYLOAD_JQ = [
    "inventory.py",
    "compare.py",
    "doctor.py",
    "policy_lint.py",
    "skill_lint.py",
    "question_lint.py",
    "trigger_lint.py",
    "trigger_eval.py",
    "peer_fill.py",
    "apply_fill.py",
    "catalog_fill.py",
    "compact.py",
    "smoke.py",
    "trace.py",
    "jev.py",
    "inventory_hook.py",
]


class JqParityTests(unittest.TestCase):
    def test_unknown_jq_key_exits_2(self) -> None:
        env = dict(os.environ)
        env["JEV_CONSULT_LOG"] = "0"
        env.pop("TYPESAFE_API_KEY", None)
        with tempfile.TemporaryDirectory() as tmp:
            for name in PAYLOAD_JQ:
                with self.subTest(script=name):
                    proc = subprocess.run(
                        _argv(name, tmp),
                        input="{}",
                        capture_output=True,
                        text=True,
                        timeout=90,
                        env=env,
                    )
                    self.assertEqual(
                        proc.returncode,
                        2,
                        "%s: rc=%d out=%s" % (name, proc.returncode, proc.stdout + proc.stderr),
                    )

    def test_known_jq_key_exits_0(self) -> None:
        env = dict(os.environ)
        env["JEV_CONSULT_LOG"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "policy_lint.py"), "--jq", "errors"],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertTrue(proc.stdout.strip().isdigit())


if __name__ == "__main__":
    unittest.main()

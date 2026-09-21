"""Docs <-> code consistency: every --flag named in docs/for-agents.md exists."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "for-agents.md"
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

FLAG_RE = re.compile(r"--[a-z][a-z-]*")


def _script_flags(path: Path) -> set:
    src = path.read_text(encoding="utf-8")
    return set(FLAG_RE.findall(src))


class DocFlagsTests(unittest.TestCase):
    def test_documented_flags_exist_in_scripts(self) -> None:
        lines = DOC.read_text(encoding="utf-8").splitlines()
        missing = []
        for line in lines:
            match = re.search(r"scripts/([a-z_]+\.py)", line)
            if not match:
                continue
            script = SCRIPTS / match.group(1)
            if not script.is_file():
                script = ROOT / "scripts" / match.group(1)
            if not script.is_file():
                missing.append("%s: script file missing" % match.group(1))
                continue
            have = _script_flags(script)
            for flag in set(FLAG_RE.findall(line)):
                if flag not in have:
                    missing.append("%s: %s documented but absent" % (match.group(1), flag))
        self.assertEqual(missing, [])

    def test_policy_lint_reads_real_policy(self) -> None:
        policy = json.loads(
            (ROOT / "skills" / "jev-consult" / "policy.json").read_text(encoding="utf-8")
        )
        self.assertIsInstance(policy, dict)


if __name__ == "__main__":
    unittest.main(verbosity=2)

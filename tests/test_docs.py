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

    def test_skill_md_flags_exist_in_scripts(self) -> None:
        skill_md = ROOT / "skills" / "jev-consult" / "SKILL.md"
        lines = skill_md.read_text(encoding="utf-8").splitlines()
        missing = []
        for line in lines:
            names = re.findall(r"(?:scripts/)?([a-z_]+\.py)", line)
            if not names:
                continue
            have: set[str] = set()
            for name in names:
                script = SCRIPTS / name
                if not script.is_file():
                    script = ROOT / "scripts" / name
                if script.is_file():
                    have |= _script_flags(script)
            for flag in set(FLAG_RE.findall(line)):
                # --force/--yes/--no-enable belong to external CLIs
                # (hermes skills install, claude plugin) quoted in prose.
                if flag in {"--force", "--yes", "--no-enable"}:
                    continue
                if flag not in have:
                    missing.append("SKILL.md %s: %s absent" % (names[0], flag))
        self.assertEqual(missing, [])

    def test_env_vars_used_in_scripts_are_documented(self) -> None:
        env_re = re.compile(r'(?:environ\.(?:get|getenv)|environ)\s*\(?\s*\[?\s*"(JEV_[A-Z_]+|TYPESAFE_[A-Z_]+|HERMES_HOME)"')
        used: set[str] = set()
        for script in SCRIPTS.glob("*.py"):
            used |= set(env_re.findall(script.read_text(encoding="utf-8")))
        text = (
            ROOT / "skills" / "jev-consult" / "SKILL.md"
        ).read_text(encoding="utf-8")
        undocumented = sorted(name for name in used if name not in text)
        self.assertEqual(undocumented, [])

    def test_policy_lint_reads_real_policy(self) -> None:
        policy = json.loads(
            (ROOT / "skills" / "jev-consult" / "policy.json").read_text(encoding="utf-8")
        )
        self.assertIsInstance(policy, dict)


if __name__ == "__main__":
    unittest.main(verbosity=2)

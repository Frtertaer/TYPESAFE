#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "jev-consult" / "scripts" / "skill_lint.py"

spec = importlib.util.spec_from_file_location("skill_lint", SCRIPT)
skill_lint = importlib.util.module_from_spec(spec)
sys.modules["skill_lint"] = skill_lint
spec.loader.exec_module(skill_lint)


def write_skill(tmp: str, name: str, frontmatter: str) -> Path:
    d = Path(tmp) / name
    d.mkdir(parents=True)
    path = d / "SKILL.md"
    path.write_text(frontmatter, encoding="utf-8")
    return path


GOOD = "---\nname: {name}\ndescription: A test skill.\n---\n\n# body\n"


class LintSkillTests(unittest.TestCase):
    def test_clean_skill_no_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(tmp, "jwt-auth", GOOD.format(name="jwt-auth"))
            self.assertEqual(skill_lint.lint_skill(path), [])

    def test_missing_file_error(self):
        findings = skill_lint.lint_skill(Path("nope/SKILL.md"))
        self.assertEqual(findings[0]["rule"], "S001")
        self.assertEqual(findings[0]["severity"], "error")

    def test_no_frontmatter_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(tmp, "x", "# just a body\n")
            findings = skill_lint.lint_skill(path)
            self.assertEqual(findings[0]["rule"], "S002")

    def test_missing_name_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(tmp, "x", "---\ndescription: d\n---\n")
            findings = skill_lint.lint_skill(path)
            self.assertTrue(any(f["rule"] == "S003" for f in findings))

    def test_missing_description_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(tmp, "x", "---\nname: x\n---\n")
            findings = skill_lint.lint_skill(path)
            self.assertTrue(
                any(f["rule"] == "S004" and f["severity"] == "warn" for f in findings)
            )

    def test_name_dir_mismatch_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(tmp, "actual-dir", GOOD.format(name="other-name"))
            findings = skill_lint.lint_skill(path)
            self.assertTrue(any(f["rule"] == "S005" for f in findings))

    def test_long_description_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            fm = "---\nname: x\ndescription: %s\n---\n" % ("d" * 1100)
            path = write_skill(tmp, "x", fm)
            findings = skill_lint.lint_skill(path)
            self.assertTrue(any(f["rule"] == "S006" for f in findings))

    def test_repo_skill_lints_clean(self):
        findings = skill_lint.lint_skill(
            ROOT / "skills" / "jev-consult" / "SKILL.md"
        )
        self.assertEqual([f for f in findings if f["severity"] == "error"], [])


class CliTests(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )

    def test_no_args_rc2(self):
        self.assertEqual(self._run().returncode, 2)

    def test_clean_rc0(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(tmp, "jwt-auth", GOOD.format(name="jwt-auth"))
            proc = self._run(str(path))
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_error_rc1(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(tmp, "x", "---\ndescription: d\n---\n")
            proc = self._run(str(path))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("S003", proc.stdout)

    def test_missing_rc1(self):
        proc = self._run("nope/SKILL.md")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("S001", proc.stdout)

    def test_dir_arg_lints_all_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_skill(tmp, "a", GOOD.format(name="a"))
            write_skill(tmp, "b", "# no frontmatter\n")
            proc = self._run(tmp)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("S002", proc.stdout)
            self.assertIn(str(Path(tmp) / "b" / "SKILL.md"), proc.stdout)

    def test_dir_arg_all_clean_rc0(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_skill(tmp, "a", GOOD.format(name="a"))
            write_skill(tmp, "b", GOOD.format(name="b"))
            proc = self._run(tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "")

    def test_multiple_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = write_skill(tmp, "ok", GOOD.format(name="ok"))
            bad = write_skill(tmp, "bad", "# nope\n")
            proc = self._run(str(good), str(bad))
            self.assertEqual(proc.returncode, 1)
            self.assertIn(str(bad), proc.stdout)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(verbosity=2) else 1)

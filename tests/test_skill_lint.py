#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_cited_script_missing_warn_s009(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(
                tmp,
                "x",
                GOOD.format(name="x") + "\nRun `scripts/missing.py` first.\n",
            )
            (path.parent / "scripts").mkdir()
            (path.parent / "scripts" / "real.py").write_text("# ok\n")
            findings = skill_lint.lint_skill(path)
            rules = {f["rule"] for f in findings}
            self.assertIn("S009", rules)
            self.assertTrue(
                any(
                    f["rule"] == "S009" and "missing.py" in f["message"]
                    for f in findings
                )
            )

    def test_cited_script_present_no_s009(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(
                tmp,
                "x",
                GOOD.format(name="x") + "\nRun `scripts/real.py` first.\n",
            )
            scripts = path.parent / "scripts"
            scripts.mkdir()
            (scripts / "real.py").write_text("# ok\n")
            findings = skill_lint.lint_skill(path)
            self.assertNotIn("S009", {f["rule"] for f in findings})

    def test_no_scripts_dir_no_s009(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(
                tmp,
                "x",
                GOOD.format(name="x") + "\nRun `scripts/missing.py` first.\n",
            )
            # no sibling scripts/ dir — nothing to check citations against
            findings = skill_lint.lint_skill(path)
            self.assertNotIn("S009", {f["rule"] for f in findings})

    def test_unmentioned_script_warns_s010(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(
                tmp,
                "x",
                GOOD.format(name="x") + "\nRun `scripts/real.py` first.\n",
            )
            scripts = path.parent / "scripts"
            scripts.mkdir()
            (scripts / "real.py").write_text("# ok\n")
            (scripts / "ghost.py").write_text("# never cited\n")
            findings = skill_lint.lint_skill(path)
            self.assertTrue(
                any(
                    f["rule"] == "S010" and "ghost.py" in f["message"]
                    for f in findings
                )
            )

    def test_s010_skips_private_vendored_and_mentioned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(
                tmp,
                "x",
                GOOD.format(name="x") + "\nRuns helper.py inline.\n",
            )
            scripts = path.parent / "scripts"
            scripts.mkdir()
            (scripts / "helper.py").write_text("# cited by bare name\n")
            (scripts / "_shared.py").write_text("# private module\n")
            (scripts / "vendored.py").write_text(
                "# [vendored] origin: elsewhere\n"
            )
            findings = skill_lint.lint_skill(path)
            self.assertNotIn("S010", {f["rule"] for f in findings})

    def test_undocumented_flag_fires_s012(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(
                tmp,
                "x",
                GOOD.format(name="x") + "\nRun `scripts/real.py` first.\n",
            )
            scripts = path.parent / "scripts"
            scripts.mkdir()
            (scripts / "real.py").write_text(
                "import argparse\np = argparse.ArgumentParser()\np.add_argument('--verbose-foo')\n",
                encoding="utf-8",
            )
            findings = skill_lint.lint_skill(path)
            self.assertTrue(
                any(
                    f["rule"] == "S012" and "--verbose-foo" in f["message"]
                    for f in findings
                )
            )

    def test_documented_flag_no_s012(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(
                tmp,
                "x",
                GOOD.format(name="x") + "\n`scripts/real.py` takes `--verbose-foo`.\n",
            )
            scripts = path.parent / "scripts"
            scripts.mkdir()
            (scripts / "real.py").write_text(
                "import argparse\np = argparse.ArgumentParser()\np.add_argument('--verbose-foo')\n",
                encoding="utf-8",
            )
            findings = skill_lint.lint_skill(path)
            self.assertNotIn("S012", {f["rule"] for f in findings})

    def test_s013_fires_on_documented_but_missing_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(
                tmp,
                "x",
                GOOD.format(name="x") + "\n`scripts/real.py` takes `--ghost-flag`.\n",
            )
            scripts = path.parent / "scripts"
            scripts.mkdir()
            (scripts / "real.py").write_text(
                "import argparse\np = argparse.ArgumentParser()\np.add_argument('--real-flag')\n",
                encoding="utf-8",
            )
            findings = skill_lint.lint_skill(path)
            s013 = [f for f in findings if f["rule"] == "S013"]
            self.assertEqual(len(s013), 1)
            self.assertIn("--ghost-flag", s013[0]["message"])
            self.assertEqual(s013[0]["severity"], "warn")

    def test_s013_quiet_when_flag_exposed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(
                tmp,
                "x",
                GOOD.format(name="x") + "\n`scripts/real.py` takes `--real-flag`.\n",
            )
            scripts = path.parent / "scripts"
            scripts.mkdir()
            (scripts / "real.py").write_text(
                "import argparse\np = argparse.ArgumentParser()\np.add_argument('--real-flag')\n",
                encoding="utf-8",
            )
            findings = skill_lint.lint_skill(path)
            self.assertNotIn("S013", {f["rule"] for f in findings})

    def test_s012_ignores_unmentioned_and_subprocess_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(
                tmp,
                "x",
                GOOD.format(name="x") + "\nRun `scripts/real.py`.\n",
            )
            scripts = path.parent / "scripts"
            scripts.mkdir()
            (scripts / "real.py").write_text(
                "import argparse\np = argparse.ArgumentParser()\np.add_argument('--help-me')\n",
                encoding="utf-8",
            )
            (scripts / "ghost.py").write_text(
                "import argparse\np = argparse.ArgumentParser()\np.add_argument('--ghost-flag')\n",
                encoding="utf-8",
            )
            findings = skill_lint.lint_skill(path)
            s012 = [f["message"] for f in findings if f["rule"] == "S012"]
            self.assertTrue(any("--help-me" in m for m in s012))
            self.assertFalse(any("ghost-flag" in m for m in s012))
            self.assertFalse(any(m.startswith("--no-") for m in s012))

    def test_no_scripts_dir_no_s010(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(tmp, "x", GOOD.format(name="x"))
            findings = skill_lint.lint_skill(path)
            self.assertNotIn("S010", {f["rule"] for f in findings})

    def test_quiet_suppresses_warn_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            warn_path = write_skill(tmp, "x", "---\nname: x\n---\n")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = skill_lint.main([str(warn_path), "--quiet"])
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue(), "")

    def test_out_writes_findings_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            warn_path = write_skill(tmp, "x", "---\nname: x\n---\n")
            out_path = Path(tmp) / "findings.json"
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = skill_lint.main([str(warn_path), "--out", str(out_path)])
            self.assertEqual(rc, 0)
            self.assertIn("wrote", err.getvalue())
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertTrue(any(f["rule"] == "S004" for f in payload["findings"]))
            with contextlib.redirect_stderr(io.StringIO()):
                rc = skill_lint.main([str(warn_path), "--out"])
            self.assertEqual(rc, 2)

    def test_self_test_finds_s002(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = skill_lint.main(["--self-test"])
        self.assertEqual(rc, 0)
        self.assertIn("self-test: ok", buf.getvalue())
        self.assertIn("S002", buf.getvalue())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = skill_lint.main(["--self-test", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["self_test"], "ok")
        self.assertIn("S002", payload["rules"])

    def test_severity_filters_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            warn_path = write_skill(tmp, "x", "---\nname: x\n---\n")
            bad = Path(tmp) / "nope" / "SKILL.md"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = skill_lint.main(
                    [str(warn_path), str(bad), "--severity", "warn"]
                )
            out = buf.getvalue()
            self.assertEqual(rc, 1)  # error still counts for rc
            self.assertIn("S004", out)
            self.assertNotIn("S001", out)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = skill_lint.main([str(bad), "--severity", "bogus"])
            self.assertEqual(rc, 2)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = skill_lint.main(
                    [str(warn_path), str(bad), "--severity", "warn,error"]
                )
            out = buf.getvalue()
            self.assertEqual(rc, 1)
            self.assertIn("S004", out)
            self.assertIn("S001", out)

    def test_only_filters_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            warn_path = write_skill(tmp, "x", "---\nname: x\n---\n")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = skill_lint.main([str(warn_path), "--only", "S004"])
            out = buf.getvalue()
            self.assertIn("S004", out)
            self.assertNotIn("S001", out)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = skill_lint.main([str(warn_path), "--only", "S999"])
            self.assertEqual(rc, 2)

    def test_severity_env_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            warn_path = write_skill(tmp, "x", "---\nname: x\n---\n")
            bad = Path(tmp) / "nope" / "SKILL.md"
            with mock.patch.dict(os.environ, {"JEV_SLINT_SEVERITY": "warn"}):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = skill_lint.main([str(warn_path), str(bad)])
            out = buf.getvalue()
            self.assertEqual(rc, 1)
            self.assertIn("S004", out)
            self.assertNotIn("S001", out)
            with mock.patch.dict(os.environ, {"JEV_SLINT_SEVERITY": "warn"}):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = skill_lint.main([str(warn_path), str(bad), "--severity", "error"])
            out = buf.getvalue()
            self.assertIn("S001", out)
            self.assertNotIn("S004", out)

    def test_quiet_still_prints_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "nope" / "SKILL.md"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = skill_lint.main([str(bad), "--quiet"])
            self.assertEqual(rc, 1)
            self.assertIn("S001", buf.getvalue())

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

    def test_name_casing_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_skill(root, "MySkill", GOOD.format(name="MySkill"))
            findings = skill_lint.lint_skill(path)
            rules = [f["rule"] for f in findings]
            self.assertIn("S008", rules)

    def test_fix_normalizes_casing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_skill(root, "MySkill", GOOD.format(name="MySkill"))
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                rc = skill_lint.main([str(path), "--fix"])
            self.assertEqual(rc, 0)
            self.assertIn("name: myskill", path.read_text(encoding="utf-8"))
            self.assertIn("fixed S008", buf.getvalue())

    def test_fix_dry_run_reports_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_skill(root, "MySkill", GOOD.format(name="MySkill"))
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                rc = skill_lint.main([str(path), "--fix", "--dry-run"])
            self.assertEqual(rc, 0)
            self.assertIn("would fix S008", buf.getvalue())
            self.assertIn("name: MySkill", path.read_text(encoding="utf-8"))

    def test_long_description_s006(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = GOOD.format(name="s").replace(
                "description: A test skill.", "description: " + ("x" * 1100)
            )
            path = write_skill(root, "s", body)
            findings = skill_lint.lint_skill(path)
            rules = {f["rule"] for f in findings}
            self.assertIn("S006", rules)

    def test_name_clean_casing_no_s008(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_skill(root, "my-skill-2", GOOD.format(name="my-skill-2"))
            findings = skill_lint.lint_skill(path)
            self.assertNotIn("S008", [f["rule"] for f in findings])

    def test_policy_key_drift_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_skill(root, "s", GOOD.format(name="s"))
            path.write_text(
                path.read_text(encoding="utf-8")
                + "\nSee `ghost_key` (policy.json, default 3).\n",
                encoding="utf-8",
            )
            (root / "s" / "policy.json").write_text('{"version": 1}', encoding="utf-8")
            findings = skill_lint.lint_skill(path)
            rules = [f["rule"] for f in findings]
            self.assertIn("S007", rules)
            msg = [f["message"] for f in findings if f["rule"] == "S007"][0]
            self.assertIn("ghost_key", msg)

    def test_policy_key_present_no_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_skill(root, "s", GOOD.format(name="s"))
            path.write_text(
                path.read_text(encoding="utf-8")
                + "\nSee `version` (policy.json) here.\n",
                encoding="utf-8",
            )
            (root / "s" / "policy.json").write_text('{"version": 1}', encoding="utf-8")
            self.assertEqual(skill_lint.lint_skill(path), [])

    def test_no_policy_file_no_s007(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_skill(root, "s", GOOD.format(name="s"))
            path.write_text(
                path.read_text(encoding="utf-8")
                + "\nSee `ghost_key` (policy.json).\n",
                encoding="utf-8",
            )
            self.assertEqual(skill_lint.lint_skill(path), [])

    def test_unmentioned_policy_key_warns_s011(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_skill(root, "s", GOOD.format(name="s"))
            path.write_text(
                path.read_text(encoding="utf-8")
                + "\nThe `version` key lives in policy.json.\n",
                encoding="utf-8",
            )
            (root / "s" / "policy.json").write_text(
                '{"version": 1, "extra_knob": true}', encoding="utf-8"
            )
            findings = skill_lint.lint_skill(path)
            s011 = [f for f in findings if f["rule"] == "S011"]
            self.assertEqual(len(s011), 1)
            self.assertIn("extra_knob", s011[0]["message"])
            self.assertNotIn("version", s011[0]["message"])

    def test_all_policy_keys_mentioned_no_s011(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_skill(root, "s", GOOD.format(name="s"))
            path.write_text(
                path.read_text(encoding="utf-8") + "\n`version` is read.\n",
                encoding="utf-8",
            )
            (root / "s" / "policy.json").write_text(
                '{"version": 1}', encoding="utf-8"
            )
            self.assertEqual(
                [f for f in skill_lint.lint_skill(path) if f["rule"] == "S011"],
                [],
            )

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

    def test_jsonl_emits_one_finding_per_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = write_skill(tmp, "bad_dir", GOOD.format(name="Other"))
            proc = self._run(str(bad), "--jsonl")
            self.assertNotEqual(proc.returncode, 2)
            rows = [
                json.loads(l) for l in proc.stdout.splitlines() if l.strip()
            ]
            self.assertTrue(rows)
            for row in rows:
                self.assertEqual(row["path"], str(bad))
                self.assertIn("rule", row)
                self.assertIn("severity", row)
            self.assertNotIn("findings", proc.stdout)

    def test_jsonl_keys_projects_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = write_skill(tmp, "bad_dir", GOOD.format(name="Other"))
            proc = self._run(str(bad), "--jsonl", "--keys", "rule,path")
            self.assertNotEqual(proc.returncode, 2)
            rows = [
                json.loads(l) for l in proc.stdout.splitlines() if l.strip()
            ]
            self.assertTrue(rows)
            for row in rows:
                self.assertEqual(set(row), {"rule", "path"})
            proc = self._run(str(bad), "--jsonl", "--keys", " ,")
            self.assertEqual(proc.returncode, 2)

    def test_baseline_suppresses_known_findings(self):
        """--baseline PATH suppresses recorded findings; new ones still fire."""
        with tempfile.TemporaryDirectory() as tmp:
            bad = write_skill(tmp, "BadCase", GOOD.format(name="BadCase"))
            base = Path(tmp) / "base.json"
            proc = self._run(str(bad), "--baseline-write", str(base))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(base.is_file())
            # strict now passes: both warns are recorded
            proc = self._run(str(bad), "--baseline", str(base), "--strict")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("suppressed", proc.stderr)
            # a new finding not in the baseline still fails
            bad.write_text("---\ndescription: no name\n---\n", encoding="utf-8")
            proc = self._run(str(bad), "--baseline", str(base), "--strict")
            self.assertEqual(proc.returncode, 1)
            self.assertIn("S003", proc.stdout)

    def test_baseline_missing_or_corrupt_counts_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = write_skill(tmp, "BadCase", GOOD.format(name="BadCase"))
            proc = self._run(
                str(bad), "--baseline", str(Path(tmp) / "nope.json"),
                "--strict",
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("not found", proc.stderr)
            corrupt = Path(tmp) / "bad.json"
            corrupt.write_text("{oops", encoding="utf-8")
            proc = self._run(str(bad), "--baseline", str(corrupt), "--strict")
            self.assertEqual(proc.returncode, 1)
            self.assertIn("unreadable", proc.stderr)

    def test_baseline_dangling_flag_rc2(self):
        self.assertEqual(self._run("--baseline").returncode, 2)
        self.assertEqual(self._run("--baseline-write").returncode, 2)

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

    def test_fix_rewrites_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(tmp, "actual", GOOD.format(name="wrong"))
            proc = self._run(str(path), "--fix")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("fixed S005", proc.stderr)
            self.assertIn("name: actual", path.read_text(encoding="utf-8"))
            self.assertNotIn("S005", proc.stdout)

    def test_fix_no_name_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(tmp, "x", "---\ndescription: d\n---\n")
            proc = self._run(str(path), "--fix")
            self.assertEqual(proc.returncode, 1)
            self.assertNotIn("fixed S005", proc.stderr)

    def test_json_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = write_skill(tmp, "bad", "---\ndescription: d\n---\n")
            proc = self._run(str(bad), "--json")
            self.assertEqual(proc.returncode, 1)
            findings = json.loads(proc.stdout)["findings"]
            self.assertEqual(findings[0]["rule"], "S003")
            self.assertIn(str(bad), findings[0]["path"])

    def test_strict_warn_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            # S005 warn: dir "other" but name "wrong"
            other = write_skill(tmp, "other", GOOD.format(name="wrong"))
            proc = self._run(str(other), "--strict")
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertIn("S005", proc.stdout)
            normal = self._run(str(other))
            self.assertEqual(normal.returncode, 0, normal.stderr)

    def test_strict_json_warn_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(tmp, "other", GOOD.format(name="wrong"))
            proc = self._run(str(path), "--strict", "--json")
            self.assertEqual(proc.returncode, 1)
            rows = json.loads(proc.stdout)["findings"]
            self.assertEqual(rows[0]["rule"], "S005")

    def test_strict_clean_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_skill(tmp, "ok", GOOD.format(name="ok"))
            proc = self._run(str(path), "--strict")
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_summary_line_on_multiple_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad1 = write_skill(tmp, "bad1", "# nope\n")
            bad2 = write_skill(tmp, "bad2", "# nope\n")
            proc = self._run(str(bad1), str(bad2))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("2 findings (2 errors, 0 warns) in 2 files", proc.stdout)

    def test_no_summary_single_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = write_skill(tmp, "bad", "# nope\n")
            proc = self._run(str(bad))
            self.assertEqual(proc.returncode, 1)
            self.assertNotIn("findings (", proc.stdout)

    def test_no_summary_when_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = write_skill(tmp, "a", GOOD.format(name="a"))
            b = write_skill(tmp, "b", GOOD.format(name="b"))
            proc = self._run(str(a), str(b))
            self.assertEqual(proc.returncode, 0)
            self.assertNotIn("findings", proc.stdout)

    def test_json_clean_rc0_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = write_skill(tmp, "ok", GOOD.format(name="ok"))
            proc = self._run(str(good), "--json")
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(json.loads(proc.stdout)["findings"], [])

    def test_multiple_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = write_skill(tmp, "ok", GOOD.format(name="ok"))
            bad = write_skill(tmp, "bad", "# nope\n")
            proc = self._run(str(good), str(bad))
            self.assertEqual(proc.returncode, 1)
            self.assertIn(str(bad), proc.stdout)

    def test_watch_emits_ticks(self):
        import os as _os

        with tempfile.TemporaryDirectory() as tmp:
            good = write_skill(tmp, "ok", GOOD.format(name="ok"))
            env = dict(_os.environ, JEV_SLINT_WATCH_MAX="2")
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), str(good), "--watch", "0.01"],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                env=env,
            )
        self.assertEqual(proc.returncode, 0)
        ticks = [
            json.loads(l)
            for l in proc.stdout.splitlines()
            if l.startswith("{")
        ]
        self.assertEqual(len(ticks), 2)
        self.assertTrue(all("findings" in t and "errors" in t for t in ticks))
        self.assertTrue(all("warnings" in t and "infos" in t for t in ticks))
        stderr_lines = [
            l for l in proc.stderr.splitlines() if l.startswith("watch tick=")
        ]
        self.assertEqual(len(stderr_lines), 2)
        self.assertIn("findings=", stderr_lines[0])
        self.assertIn("errors=", stderr_lines[0])

    def test_watch_fail_fast_breaks_on_error_tick(self):
        import os as _os

        with tempfile.TemporaryDirectory() as tmp:
            bad = write_skill(tmp, "bad", "# nope\n")
            env = dict(_os.environ, JEV_SLINT_WATCH_MAX="5")
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), str(bad), "--watch", "0.01",
                 "--fail-fast"],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                env=env,
            )
        self.assertEqual(proc.returncode, 1)
        ticks = [
            json.loads(l)
            for l in proc.stdout.splitlines()
            if l.startswith("{")
        ]
        self.assertEqual(len(ticks), 1)

    def test_watch_rc_reflects_last_lint(self):
        import os as _os

        with tempfile.TemporaryDirectory() as tmp:
            bad = write_skill(tmp, "bad", "# nope\n")
            env = dict(_os.environ, JEV_SLINT_WATCH_MAX="1")
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), str(bad), "--watch", "0.01"],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                env=env,
            )
            self.assertEqual(proc.returncode, 1)
            good = write_skill(tmp, "ok", GOOD.format(name="ok"))
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), str(good), "--watch", "0.01"],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                env=env,
            )
            self.assertEqual(proc.returncode, 0)

    def test_watch_verdict_writes_final_state(self):
        import os as _os

        with tempfile.TemporaryDirectory() as tmp:
            good = write_skill(tmp, "ok", GOOD.format(name="ok"))
            verdict = Path(tmp) / "v.json"
            env = dict(_os.environ, JEV_SLINT_WATCH_MAX="2")
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), str(good), "--watch", "0.01",
                 "--verdict", str(verdict)],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "pass")
            self.assertEqual(payload["ticks"], 2)
            self.assertEqual(payload["errors"], 0)

    def test_nonwatch_verdict_writes_single_shot(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = write_skill(tmp, "ok", GOOD.format(name="ok"))
            verdict = Path(tmp) / "v.json"
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), str(good), "--verdict", str(verdict)],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "pass")
            self.assertEqual(payload["ticks"], 1)
            self.assertEqual(payload["errors"], 0)

    def test_watch_appends_ticks_to_out_file(self):
        import os as _os

        with tempfile.TemporaryDirectory() as tmp:
            good = write_skill(tmp, "ok", GOOD.format(name="ok"))
            out = Path(tmp) / "ticks.jsonl"
            env = dict(_os.environ, JEV_SLINT_WATCH_MAX="2")
            proc = subprocess.run(
                [
                    sys.executable, str(SCRIPT), str(good),
                    "--watch", "0.01", "--out", str(out),
                ],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                env=env,
            )
            self.assertEqual(proc.returncode, 0)
            lines = [
                json.loads(l)
                for l in out.read_text(encoding="utf-8").splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(lines), 2)
            self.assertTrue(all("findings" in t for t in lines))



    def test_explain_prints_rule_description(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = skill_lint.main(["--explain", "S007"])
        self.assertEqual(rc, 0)
        self.assertIn("S007:", buf.getvalue())
        self.assertIn("policy.json", buf.getvalue())

    def test_explain_unknown_rule_rc2(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = skill_lint.main(["--explain", "S999"])
        self.assertEqual(rc, 2)


class WatchJqTests(unittest.TestCase):
    def test_watch_jq_prints_only_named_tick_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "SKILL.md"
            skill.write_text(
                "---" + chr(10) + "name: x" + chr(10) + "description: y" + chr(10) + "---" + chr(10) + "body" + chr(10),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with mock.patch.dict(os.environ, {"JEV_SLINT_WATCH_MAX": "2"}):
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                    rc = skill_lint.main(
                        [str(skill), "--watch", "0.01", "--jq", "errors"]
                    )
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().splitlines(), ["0", "0"])

class WatchSecsEnvTests(unittest.TestCase):
    def test_watch_secs_env_bounds_loop(self):
        import os as _os
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            good = write_skill(tmp, "ok", GOOD.format(name="ok"))
            env = dict(
                _os.environ,
                JEV_SLINT_WATCH_MAX="0",
                JEV_SLINT_WATCH_SECS="0.05",
            )
            start = _time.time()
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), str(good), "--watch", "0.02"],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                env=env,
                timeout=30,
            )
            self.assertLess(_time.time() - start, 10.0)
            ticks = [
                l for l in proc.stdout.splitlines() if l.startswith("{")
            ]
            self.assertLessEqual(len(ticks), 10)
            self.assertGreaterEqual(len(ticks), 1)


class ShippedSkillLintTests(unittest.TestCase):
    """The pack's own skills/jev-consult/SKILL.md must stay lint-clean."""

    def test_shipped_skill_lints_clean(self) -> None:
        rc = skill_lint.main(
            [str(ROOT / "skills" / "jev-consult" / "SKILL.md"), "--severity", "warn"]
        )
        self.assertEqual(rc, 0)


class FixtureSkillLintTests(unittest.TestCase):
    """Every tests/fixtures SKILL.md must stay lint-clean — the fixtures
    stand in for real installed skills across the suite."""

    def test_fixture_skills_are_lint_clean(self) -> None:
        fixtures = sorted(
            (ROOT / "tests" / "fixtures").rglob("SKILL.md")
        )
        self.assertTrue(fixtures, "no fixture SKILL.md files found")
        for path in fixtures:
            with self.subTest(fixture=path.name):
                rc = skill_lint.main([str(path), "--severity", "warn"])
                self.assertEqual(rc, 0, "%s has lint errors" % path)

class RulesCatalogTest(unittest.TestCase):
    def test_rules_lists_every_rule_sorted(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = skill_lint.main(["--rules"])
        self.assertEqual(rc, 0)
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), len(skill_lint.RULES))
        self.assertIn("S001:", lines[0])

    def test_rules_json_shape(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = skill_lint.main(["--rules", "--json"])
        self.assertEqual(rc, 0)
        rows = json.loads(buf.getvalue())
        self.assertEqual(sorted(r["rule"] for r in rows), sorted(skill_lint.RULES))

    def test_schema_prints_frontmatter_contract(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = skill_lint.main(["--schema"])
        self.assertEqual(rc, 0)
        self.assertIn("name: slug", buf.getvalue())
        self.assertIn("(required)", buf.getvalue())
        self.assertIn("description:", buf.getvalue())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = skill_lint.main(["--schema", "--json"])
        self.assertEqual(rc, 0)
        rows = json.loads(buf.getvalue())
        self.assertTrue(rows["name"]["required"])
        self.assertFalse(rows["description"]["required"])


class StdinDashTests(unittest.TestCase):
    """'-' path reads the SKILL.md text from stdin (cached)."""

    def setUp(self) -> None:
        self._prev = skill_lint._STDIN_MD
        skill_lint._STDIN_MD = None

    def tearDown(self) -> None:
        skill_lint._STDIN_MD = self._prev

    def _feed(self, argv: list, stdin_text: str):
        buf = io.StringIO()
        err = io.StringIO()
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            # '-' resolves sibling context (policy.json, scripts/) from cwd;
            # an empty cwd keeps the fixture hermetic.
            os.chdir(tmp)
            try:
                with mock.patch("sys.stdin", io.StringIO(stdin_text)):
                    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                        rc = skill_lint.main(argv)
            finally:
                os.chdir(cwd)
        return rc, buf.getvalue(), err.getvalue()

    def test_stdin_clean_no_findings(self):
        rc, out, _ = self._feed(["-", "--json"], GOOD.format(name="x"))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["findings"], [])

    def test_stdin_bad_skill_reports_dash_path(self):
        rc, out, _ = self._feed(["-", "--json"], "no frontmatter\n")
        self.assertEqual(rc, 1)
        rows = json.loads(out)["findings"]
        self.assertEqual(rows[0]["rule"], "S002")
        self.assertEqual(rows[0]["path"], "-")

    def test_stdin_rejects_fix_and_watch(self):
        for argv in (["-", "--fix"], ["-", "--watch", "1"]):
            rc, _out, err = self._feed(list(argv), GOOD.format(name="x"))
            self.assertEqual(rc, 2, argv)
            self.assertIn("stdin", err)

    def test_stdin_text_cached_across_calls(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with mock.patch("sys.stdin", io.StringIO(GOOD.format(name="x"))):
                    first = skill_lint.lint_skill(Path("-"))
                    second = skill_lint.lint_skill(Path("-"))
            finally:
                os.chdir(cwd)
        self.assertEqual(first, [])
        self.assertEqual(second, [])

    def test_stdin_multi_mixed_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = write_skill(tmp, "ok-one", GOOD.format(name="ok-one"))
            rc, out, _ = self._feed(
                ["-", str(good), "--json"], "no frontmatter\n"
            )
            self.assertEqual(rc, 1)
            rows = json.loads(out)["findings"]
            self.assertIn("-", {r["path"] for r in rows})
            # the clean skill contributes no rows; stdin findings carry "-"
            self.assertFalse(any(r["severity"] == "error" and r["path"].endswith("SKILL.md") for r in rows))



class DiffFlagTests(unittest.TestCase):
    """`--diff PATH` compares two SKILL.md docs; '-' reads stdin side."""

    A = "---\nname: a\ndescription: A one.\n---\nuses `scripts/jev.py`\n"
    B = ("---\nname: b\ndescription: A two.\n---\n"
         "uses `scripts/jev.py` and `scripts/decisions.py`\n")

    def _files(self, tmp: str):
        a = write_skill(tmp, "a", self.A)
        b = write_skill(tmp, "b", self.B)
        return a, b

    def _run(self, argv, stdin_text=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin_text)):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = skill_lint.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_diff_identical_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, _b = self._files(tmp)
            rc, out, _ = self._run([str(a), "--diff", str(a)])
        self.assertEqual(rc, 0)
        self.assertIn("0 difference(s)", out)

    def test_diff_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = self._files(tmp)
            rc, out, _ = self._run([str(b), "--diff", str(a)])
        self.assertEqual(rc, 0)
        self.assertIn("~ frontmatter.name: a -> b", out)
        self.assertIn("~ frontmatter.description:", out)
        self.assertIn("+ cited_scripts.decisions.py", out)
        self.assertIn("~ body_sha1:", out)

    def test_diff_dash_side_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = self._files(tmp)
            rc, out, _ = self._run([str(b), "--diff", "-"], stdin_text=self.A)
        self.assertEqual(rc, 0)
        self.assertIn("diff - ->", out)
        self.assertIn("+ cited_scripts.decisions.py", out)

    def test_diff_stdin_main_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, _b = self._files(tmp)
            rc, _o, err = self._run(["-", "--diff", str(a)], stdin_text=self.A)
        self.assertEqual(rc, 2)
        self.assertIn("--diff", err)

    def test_diff_multi_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = self._files(tmp)
            rc, _o, err = self._run([str(a), str(b), "--diff", str(a)])
        self.assertEqual(rc, 2)
        self.assertIn("--diff", err)

    def test_diff_unreadable_rc2(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, _b = self._files(tmp)
            rc, _o, err = self._run(
                [str(a), "--diff", str(Path(tmp) / "none.md")]
            )
        self.assertEqual(rc, 2)
        self.assertIn("readable", err)

    def test_init_skeleton_lints_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "my-skill"
            skill_dir.mkdir()
            doc = skill_dir / "SKILL.md"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = skill_lint.main(["--init"])
            self.assertEqual(rc, 0)
            doc.write_text(buf.getvalue(), encoding="utf-8")
            self.assertEqual(skill_lint.lint_skill(doc), [])

if __name__ == "__main__":
    sys.exit(0 if unittest.main(verbosity=2) else 1)

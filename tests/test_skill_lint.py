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

if __name__ == "__main__":
    sys.exit(0 if unittest.main(verbosity=2) else 1)

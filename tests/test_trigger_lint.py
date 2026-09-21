#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import trigger_lint  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "jev-consult.trigger-cases.json"


def write_cases(tmp: str, cases: list) -> Path:
    path = Path(tmp) / "cases.json"
    path.write_text(json.dumps({"skill": "jev-consult", "cases": cases}), encoding="utf-8")
    return path


GOOD_CASE = {"id": "pos-x", "prompt": "Decide which approach to take first", "should_trigger": True, "covers": ["approach"]}


class LintCasesTests(unittest.TestCase):
    def test_bundled_fixture_clean_of_errors(self) -> None:
        findings = trigger_lint.lint_cases(FIXTURE)
        errors = [f for f in findings if f["severity"] == "error"]
        self.assertEqual(errors, [])
        self.assertFalse(any(f["rule"] == "T011" for f in findings))

    def test_unreadable_file_t001(self) -> None:
        findings = trigger_lint.lint_cases(Path("no-such-file.json"))
        self.assertEqual(findings[0]["rule"], "T001")
        self.assertEqual(findings[0]["severity"], "error")

    def test_missing_cases_list_t002(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text("{}", encoding="utf-8")
            findings = trigger_lint.lint_cases(path)
        self.assertTrue(any(f["rule"] == "T002" for f in findings))

    def test_duplicate_and_missing_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cases(
                tmp,
                [
                    dict(GOOD_CASE),
                    {"id": "pos-x", "should_trigger": True},
                    {"id": "BADID", "prompt": "short", "should_trigger": "yes"},
                ],
            )
            findings = trigger_lint.lint_cases(path)
        rules = [f["rule"] for f in findings]
        self.assertIn("T003", rules)  # missing prompt on second case
        self.assertIn("T004", rules)  # duplicate pos-x
        self.assertIn("T005", rules)  # BADID naming
        self.assertIn("T006", rules)  # short prompt
        self.assertIn("T007", rules)  # should_trigger not bool

    def test_unknown_covers_kind_t008(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cases(
                tmp, [dict(GOOD_CASE, covers=["not_a_kind"])]
            )
            findings = trigger_lint.lint_cases(path)
        self.assertTrue(any(f["rule"] == "T008" for f in findings))

    def test_positive_without_covers_t009(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = dict(GOOD_CASE)
            del case["covers"]
            path = write_cases(tmp, [case])
            findings = trigger_lint.lint_cases(path)
        self.assertTrue(any(f["rule"] == "T009" for f in findings))

    def test_uncovered_must_ask_kind_t011(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cases(tmp, [dict(GOOD_CASE)])
            findings = trigger_lint.lint_cases(path)
        t011 = [f for f in findings if f["rule"] == "T011"]
        self.assertTrue(t011)
        self.assertTrue(all(f["severity"] == "warn" for f in t011))
        kinds = json.loads((SCRIPTS.parent / "policy.json").read_text(encoding="utf-8"))["must_ask"]
        uncovered = {f["message"] for f in t011}
        self.assertEqual(len(t011), len(set(kinds) - {"approach"}))
        self.assertTrue(all("'approach'" not in m for m in uncovered))


class CliTests(unittest.TestCase):
    def test_rc_and_footer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cases(tmp, [dict(GOOD_CASE)])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = trigger_lint.main([str(path)])
            self.assertEqual(rc, 0)
            self.assertIn("0 error(s)", buf.getvalue())

            bad = write_cases(tmp, [{"id": "pos-x", "should_trigger": True}])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = trigger_lint.main([str(bad)])
            self.assertEqual(rc, 1)

    def test_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cases(tmp, [{"id": "pos-x", "should_trigger": True}])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = trigger_lint.main([str(path), "--json"])
            self.assertEqual(rc, 1)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["errors"], 1)
            self.assertTrue(any(f["rule"] == "T003" for f in payload["findings"]))

    def test_quiet_and_severity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cases(
                tmp, [dict(GOOD_CASE), {"id": "pos-y", "prompt": "pick the approach now", "should_trigger": True}]
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = trigger_lint.main([str(path), "--quiet"])
            self.assertEqual(rc, 0)
            self.assertNotIn("T009", buf.getvalue())
            buf = io.StringIO()
            with redirect_stdout(buf):
                trigger_lint.main([str(path), "--severity", "warn"])
            self.assertIn("T009", buf.getvalue())

    def test_out_writes_findings_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cases(tmp, [{"id": "pos-x", "should_trigger": True}])
            out_path = Path(tmp) / "tlint.json"
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = trigger_lint.main([str(path), "--out", str(out_path)])
            self.assertEqual(rc, 1)
            self.assertIn("wrote", err.getvalue())
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["errors"], 1)
            self.assertTrue(any(f["rule"] == "T003" for f in payload["findings"]))
            err = io.StringIO()
            with redirect_stderr(err):
                rc = trigger_lint.main([str(path), "--out"])
            self.assertEqual(rc, 2)

    def test_policy_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "policy.json"
            policy.write_text(json.dumps({"must_ask": ["approach"]}), encoding="utf-8")
            path = write_cases(tmp, [dict(GOOD_CASE)])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = trigger_lint.main([str(path), "--policy", str(policy)])
            self.assertEqual(rc, 0)
            self.assertNotIn("T011", buf.getvalue())
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = trigger_lint.main([str(path), "--policy"])
            self.assertEqual(rc, 2)

    def test_severity_env_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cases(
                tmp, [{"id": "pos-y", "prompt": "pick the approach now", "should_trigger": True}]
            )
            with mock.patch.dict(os.environ, {"JEV_TLINT_SEVERITY": "warn"}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    trigger_lint.main([str(path)])
            self.assertIn("T009", buf.getvalue())
            with mock.patch.dict(os.environ, {"JEV_TLINT_SEVERITY": "warn"}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    trigger_lint.main([str(path), "--severity", "error"])
            self.assertNotIn("T009", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

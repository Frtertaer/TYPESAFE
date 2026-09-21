#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
JEV_PATH = SCRIPTS / "jev.py"

sys.path.insert(0, str(SCRIPTS))

import question_lint  # noqa: E402
import inventory  # noqa: E402
import catalog_fill  # noqa: E402


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


jev = load(JEV_PATH, "jev")


def rules(findings):
    return {(f["rule"], f["severity"]) for f in findings}


def noul(instructions: str, criteria=None) -> dict:
    q = {"type": "noul", "instructions": instructions}
    if criteria is not None:
        q["criteria"] = criteria
    return q


class LintQuestionTests(unittest.TestCase):
    def test_single_negation_warns_j001(self) -> None:
        findings = question_lint.lint_question("q", noul("Should the coder not proceed?"))
        self.assertIn(("J001", "warn"), rules(findings))
        self.assertNotIn(("J002", "error"), rules(findings))

    def test_double_negation_is_error_j002(self) -> None:
        findings = question_lint.lint_question(
            "q", noul("Is it not true that the fix cannot ship?")
        )
        self.assertIn(("J002", "error"), rules(findings))

    def test_noun_list_does_not_fire_j010(self) -> None:
        findings = question_lint.lint_question(
            "q",
            noul(
                "Does this user turn need one of these installed skills, "
                "plugins, or MCP servers loaded now?"
            ),
        )
        self.assertNotIn(("J010", "warn"), rules(findings))

    def test_second_auxiliary_fires_j010(self) -> None:
        findings = question_lint.lint_question(
            "q", noul("Is it done and does it pass?")
        )
        self.assertIn(("J010", "warn"), rules(findings))

    def test_and_or_fires_j010(self) -> None:
        findings = question_lint.lint_question(
            "q", noul("Should the coder keep and/or change the module?")
        )
        self.assertIn(("J010", "warn"), rules(findings))

    def test_overlap_j012_fires_on_near_identical_options(self) -> None:
        q = {
            "type": "choice",
            "instructions": "Which move should the coder take next?",
            "criteria": {
                "retry": "retry the same step again now",
                "retry_again": "retry the same step again now",
                "none": "none of these",
            },
        }
        findings = question_lint.lint_question("q", q)
        self.assertIn(("J012", "warn"), rules(findings))

    def test_j011_error_over_hard_max(self) -> None:
        q = {
            "type": "choice",
            "instructions": "Which option should the coder pick for this task?",
            "criteria": {"opt%d" % i: "option %d" % i for i in range(256)},
        }
        findings = question_lint.lint_question("q", q)
        self.assertIn(("J011", "error"), rules(findings))

    def test_j014_identical_true_false_is_error(self) -> None:
        findings = question_lint.lint_question(
            "q",
            noul(
                "Is the shortlist enough for this task to proceed safely?",
                {"true": "yes it is", "false": "YES IT IS"},
            ),
        )
        self.assertIn(("J014", "error"), rules(findings))

    def test_fat_state_is_error_j020(self) -> None:
        findings = question_lint.lint_request(
            {"state": "a " * 40000, "questions": {"q": noul("Is this fine?")}}
        )
        self.assertIn(("J020", "error"), rules(findings))

    def test_medium_state_warns_j021(self) -> None:
        findings = question_lint.lint_request(
            {"state": "a " * 10000, "questions": {"q": noul("Is this fine?")}}
        )
        self.assertIn(("J021", "warn"), rules(findings))
        self.assertNotIn(("J020", "error"), rules(findings))

    def test_findings_sorted_error_first(self) -> None:
        request = {
            "state": "a " * 10000,
            "questions": {
                "q": noul("Is it not true that the fix cannot ship?"),
            },
        }
        findings = question_lint.lint_request(request)
        severities = [f["severity"] for f in findings]
        self.assertEqual(severities, sorted(severities, key=["error", "warn", "info"].index))
        self.assertEqual(findings[0]["severity"], "error")

    def test_format_finding_shape(self) -> None:
        line = question_lint.format_finding(
            {
                "rule": "J001",
                "severity": "warn",
                "qid": "need_skill",
                "message": "negation ('not')",
                "fix": "fix it",
            }
        )
        self.assertTrue(line.startswith("warn  J001 need_skill: negation ('not')"))


class LintCliTests(unittest.TestCase):
    def _write(self, tmp: str, request: dict) -> Path:
        path = Path(tmp) / "req.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        return path

    def test_lint_cli_error_returns_1(self) -> None:
        request = {
            "state": {"task": "x"},
            "questions": {
                "q": noul("Is it not true that the fix cannot ship?"),
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, request)
            proc = subprocess.run(
                [sys.executable, str(JEV_PATH), "lint", str(path)],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
            )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("error J002", proc.stdout)
        self.assertIn("lint: 1 error(s), 0 warning(s), 1 info", proc.stdout)

    def test_lint_cli_strict_fails_on_warn_only(self) -> None:
        request = {
            "state": {"task": "x"},
            "questions": {"q": noul("Should the coder not proceed?")},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, request)
            loose = subprocess.run(
                [sys.executable, str(JEV_PATH), "lint", str(path)],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
            )
            strict = subprocess.run(
                [sys.executable, str(JEV_PATH), "lint", str(path), "--strict"],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
            )
        self.assertEqual(loose.returncode, 0)
        self.assertEqual(strict.returncode, 1)
        self.assertIn("warn  J001", strict.stdout)

    def test_lint_cli_clean_returns_0(self) -> None:
        request = {
            "state": {"task": "x"},
            "questions": {"q": noul("Should the coder proceed with the plan?")},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, request)
            proc = subprocess.run(
                [sys.executable, str(JEV_PATH), "lint", str(path)],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
            )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("lint: 0 error(s), 0 warning(s), 1 info", proc.stdout)


class PolicyLintTests(unittest.TestCase):
    def test_policy_templates_lint_clean(self) -> None:
        policy = json.loads(
            (ROOT / "skills" / "jev-consult" / "policy.json").read_text(encoding="utf-8")
        )
        bad = []
        for qid, q in policy.get("templates", {}).items():
            for finding in question_lint.lint_question(qid, q):
                if finding["severity"] in ("error", "warn"):
                    bad.append(finding)
        self.assertEqual(bad, [])

    def test_picker_request_lints_clean(self) -> None:
        request = inventory.picker_request(
            "add jwt auth",
            "hermes",
            [{"id": "s1", "kind": "skill", "name": "jwt-auth", "description": "JWT auth helpers"}],
        )
        bad = [
            f
            for qid, q in request["questions"].items()
            for f in question_lint.lint_question(qid, q)
            if f["severity"] in ("error", "warn")
        ]
        self.assertEqual(bad, [])

    def test_catalog_ask_lints_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ask.json"
            catalog_fill.write_catalog_ask(
                path,
                "add jwt auth",
                "claude-code",
                [{"id": "c1", "name": "jwt-auth", "identifier": "skills-sh/acme/jwt-auth", "description": "JWT helpers"}],
            )
            request = json.loads(path.read_text(encoding="utf-8"))
        bad = [
            f
            for qid, q in request["questions"].items()
            for f in question_lint.lint_question(qid, q)
            if f["severity"] in ("error", "warn")
        ]
        self.assertEqual(bad, [])


class AskLintWarningTests(unittest.TestCase):
    def test_cmd_ask_appends_lint_warning(self) -> None:
        request = {
            "state": {"task": "x"},
            "questions": {
                "move": {
                    "type": "choice",
                    "instructions": "Is it not true that the fix cannot ship today?",
                    "criteria": {"a": "option a", "b": "option b", "none": "none"},
                }
            },
        }
        result = {
            "model": "m",
            "answers": {
                "move": {
                    "type": "choice",
                    "choice": "a",
                    "confidence": 0.9,
                    "probabilities": {"a": 0.9, "b": 0.05, "none": 0.05},
                }
            },
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "req.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            buf = io.StringIO()
            with patch.object(jev, "post_systemone", return_value=result):
                with redirect_stdout(buf):
                    rc = jev.main(["ask", str(path)])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertTrue(
            any("lint J002" in w for w in out["warnings"]),
            out["warnings"],
        )


class RemainingRulesTests(unittest.TestCase):
    def test_arithmetic_warns_j003(self) -> None:
        findings = question_lint.lint_question(
            "q", noul("Is the number of failing tests greater than five?")
        )
        self.assertIn(("J003", "warn"), rules(findings))

    def test_datetime_warns_j004(self) -> None:
        findings = question_lint.lint_question(
            "q", noul("Did the token expire within the last hour?")
        )
        self.assertIn(("J004", "warn"), rules(findings))

    def test_numeric_warns_j005(self) -> None:
        findings = question_lint.lint_question(
            "q", noul("Does the error rate exceed 500 per minute?")
        )
        self.assertIn(("J005", "warn"), rules(findings))

    def test_multi_hop_info_j006(self) -> None:
        findings = question_lint.lint_question(
            "q",
            noul(
                "If the build passed then should the release ship, otherwise "
                "should we roll back?"
            ),
        )
        self.assertIn(("J006", "info"), rules(findings))

    def test_short_instructions_warn_j007(self) -> None:
        findings = question_lint.lint_question("q", noul("Pick one."))
        self.assertIn(("J007", "warn"), rules(findings))

    def test_vague_no_criteria_info_j008(self) -> None:
        findings = question_lint.lint_question(
            "q",
            {"type": "score", "instructions": "Is this approach good for the task?"},
        )
        self.assertIn(("J008", "info"), rules(findings))

    def test_vague_with_criteria_no_j008(self) -> None:
        findings = question_lint.lint_question(
            "q",
            noul(
                "Is this approach good for the task?",
                {"true": "meets the goal", "false": "does not meet it"},
            ),
        )
        self.assertNotIn(("J008", "info"), rules(findings))

    def test_noul_no_criteria_info_j009(self) -> None:
        findings = question_lint.lint_question(
            "q", noul("Should the worker retry the failing step now?")
        )
        self.assertIn(("J009", "info"), rules(findings))

    def test_choice_undescribed_options_j009(self) -> None:
        findings = question_lint.lint_question(
            "q",
            {
                "type": "choice",
                "instructions": "Which harness should receive this skill?",
                "criteria": {"a": "", "b": "", "c": "", "d": "described"},
            },
        )
        self.assertIn(("J009", "info"), rules(findings))

    def test_score_many_levels_warns_j013(self) -> None:
        findings = question_lint.lint_question(
            "q",
            {
                "type": "score",
                "instructions": "Rate the answer quality on a detailed scale.",
                "criteria": ["l%s" % i for i in range(8)],
            },
        )
        self.assertIn(("J013", "warn"), rules(findings))

    def test_score_five_levels_no_j013(self) -> None:
        findings = question_lint.lint_question(
            "q",
            {
                "type": "score",
                "instructions": "Rate the answer quality on a coarse scale.",
                "criteria": ["l%s" % i for i in range(5)],
            },
        )
        self.assertNotIn(("J013", "warn"), rules(findings))

    def test_clean_question_no_findings(self) -> None:
        findings = question_lint.lint_question(
            "q",
            {
                "type": "choice",
                "instructions": "Which library should handle the JWT signing?",
                "criteria": {"jwtlib": "PyJWT-based", "none": "skip this step"},
            },
        )
        self.assertEqual(findings, [])


if __name__ == "__main__":
    sys.exit(0 if unittest.main(verbosity=2) else 1)

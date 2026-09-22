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
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
JEV_PATH = SCRIPTS / "jev.py"
QLINT = SCRIPTS / "question_lint.py"

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

    def test_j015_info_on_too_few_options(self) -> None:
        findings = question_lint.lint_question(
            "q",
            {
                "type": "choice",
                "instructions": "Which option should the coder pick for this task?",
                "criteria": {},
            },
        )
        self.assertIn(("J015", "info"), rules(findings))
        findings = question_lint.lint_question(
            "q",
            {
                "type": "choice",
                "instructions": "Which option should the coder pick for this task?",
                "criteria": {"a": "pick a", "b": "pick b"},
            },
        )
        self.assertNotIn(("J015", "info"), rules(findings))

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

    def test_multi_paths_lint_each_request(self) -> None:
        good = {"state": {"task": "x"}, "questions": {"q": noul("Ship the fix?")}}
        bad = {
            "state": {"task": "x"},
            "questions": {"q": noul("Is it not true that the fix cannot ship?")},
        }
        with tempfile.TemporaryDirectory() as tmp:
            p1 = Path(tmp) / "a.json"
            p1.write_text(json.dumps(good), encoding="utf-8")
            p2 = Path(tmp) / "b.json"
            p2.write_text(json.dumps(bad), encoding="utf-8")
            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = question_lint.main([str(p1), str(p2)])
            self.assertEqual(rc, 1)
            out = buf.getvalue()
            self.assertIn(str(p1) + ":", out)
            self.assertIn(str(p2) + ":", out)
            self.assertIn("J002", out)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = question_lint.main([str(p1), str(p1)])
            self.assertEqual(rc, 0)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = question_lint.main([str(p1), str(p2), "--json"])
            self.assertEqual(rc, 1)
            rows = json.loads(buf.getvalue())
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["errors"], 0)
            self.assertEqual(rows[1]["errors"], 1)

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

    def test_script_strict_flag_fails_on_warn(self) -> None:
        request = {
            "state": {"task": "x"},
            "questions": {"q": noul("Should the coder not proceed?")},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, request)
            loose = subprocess.run(
                [sys.executable, str(QLINT), str(path)],
                capture_output=True,
                text=True,
            )
            strict = subprocess.run(
                [sys.executable, str(QLINT), str(path), "--strict"],
                capture_output=True,
                text=True,
            )
        self.assertEqual(loose.returncode, 0)
        self.assertEqual(strict.returncode, 1)

    def test_quiet_prints_only_errors(self) -> None:
        request = {
            "state": {"task": "x"},
            "questions": {"q": noul("Should the coder not proceed?")},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, request)
            proc = subprocess.run(
                [sys.executable, str(QLINT), str(path), "--quiet"],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("J001", proc.stdout)
        self.assertNotIn("lint:", proc.stdout)

    def test_severity_filters_findings(self) -> None:
        request = {
            "state": {"task": "x"},
            "questions": {
                "a": noul("Should the coder not proceed?"),
                "b": noul("Is it not true that the fix cannot ship?"),
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, request)
            proc = subprocess.run(
                [sys.executable, str(QLINT), str(path), "--severity", "warn"],
                capture_output=True,
                text=True,
            )
            self.assertIn("warn  J001", proc.stdout)
            self.assertNotIn("J002", proc.stdout)
            proc = subprocess.run(
                [sys.executable, str(QLINT), str(path), "--severity", "error"],
                capture_output=True,
                text=True,
            )
            self.assertIn("J002", proc.stdout)
            self.assertNotIn("J001", proc.stdout)
            proc = subprocess.run(
                [sys.executable, str(QLINT), str(path), "--severity", "bogus"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 2)

    def test_out_writes_findings_json(self) -> None:
        request = {
            "state": {"task": "x"},
            "questions": {
                "a": noul("Should the coder not proceed?"),
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, request)
            out_path = Path(tmp) / "findings.json"
            proc = subprocess.run(
                [sys.executable, str(QLINT), str(path), "--out", str(out_path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("wrote", proc.stderr)
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertIn("findings", payload)
            proc = subprocess.run(
                [sys.executable, str(QLINT), str(path), "--out"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 2)

    def test_severity_env_default(self) -> None:
        import os as _os

        request = {
            "state": {"task": "x"},
            "questions": {
                "a": noul("Should the coder not proceed?"),
                "b": noul("Is it not true that the fix cannot ship?"),
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, request)
            env = dict(_os.environ, JEV_QLINT_SEVERITY="warn")
            proc = subprocess.run(
                [sys.executable, str(QLINT), str(path)],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertIn("J001", proc.stdout)
            self.assertNotIn("J002", proc.stdout)
            # CLI flag beats env
            proc = subprocess.run(
                [sys.executable, str(QLINT), str(path), "--severity", "error"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertIn("J002", proc.stdout)
            self.assertNotIn("J001", proc.stdout)

    def test_quiet_still_prints_errors(self) -> None:
        request = {
            "state": {"task": "x"},
            "questions": {
                "q": noul("Is it not true that the fix cannot ship?"),
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, request)
            proc = subprocess.run(
                [sys.executable, str(QLINT), str(path), "--quiet"],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("J002", proc.stdout)

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


class StandaloneCliTests(unittest.TestCase):
    QLINT_PATH = SCRIPTS / "question_lint.py"

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.QLINT_PATH), *args],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )

    def test_no_args_rc2(self) -> None:
        self.assertEqual(self._run().returncode, 2)

    def test_clean_request_rc0(self) -> None:
        request = {
            "state": {"task": "x"},
            "questions": {"q": noul("Should the coder proceed with the plan?")},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "req.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            proc = self._run(str(path))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("lint: 1 finding(s)", proc.stdout)

    def test_error_request_rc1(self) -> None:
        request = {
            "state": "1" * 100000,
            "questions": {"q": noul("Should the coder proceed with the plan?")},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "req.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            proc = self._run(str(path))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("error J020", proc.stdout)

    def test_json_flag(self) -> None:
        request = {
            "state": "1" * 100000,
            "questions": {"q": noul("Should the coder proceed with the plan?")},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "req.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            proc = self._run(str(path), "--json")
        self.assertEqual(proc.returncode, 1)
        findings = json.loads(proc.stdout)["findings"]
        self.assertTrue(any(f["severity"] == "error" for f in findings))

    def test_missing_file_rc2(self) -> None:
        self.assertEqual(self._run("nonexistent.json").returncode, 2)

    def test_fix_fills_empty_choice_descriptions(self) -> None:
        request = {
            "state": {"task": "x"},
            "questions": {
                "q": {
                    "type": "choice",
                    "instructions": "Which library should handle the JWT signing?",
                    "criteria": {"jwtlib": "", "other": "", "none": "skip"},
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "req.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            proc = self._run(str(path), "--fix")
            fixed = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("fixed J009", proc.stderr)
        criteria = fixed["questions"]["q"]["criteria"]
        self.assertEqual(criteria["jwtlib"], "jwtlib")
        self.assertEqual(criteria["other"], "other")
        self.assertEqual(criteria["none"], "skip")

    def test_fix_noul_identical_criteria(self) -> None:
        request = {
            "state": {"task": "x"},
            "questions": {
                "q": {
                    "type": "noul",
                    "instructions": "Should the coder proceed with the plan?",
                    "criteria": {"true": "same", "false": "same"},
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "req.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            proc = self._run(str(path), "--fix")
            fixed = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("fixed J014", proc.stderr)
        criteria = fixed["questions"]["q"]["criteria"]
        self.assertNotEqual(criteria["true"], criteria["false"])

    def test_fix_no_op_when_clean(self) -> None:
        request = {
            "state": {"task": "x"},
            "questions": {"q": noul("Should the coder proceed with the plan?")},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "req.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            proc = self._run(str(path), "--fix")
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("fixed", proc.stderr)

    def test_fix_unfixable_finding_still_lints(self) -> None:
        request = {
            "state": {"task": "x"},
            "questions": {"q": noul("Is it not true?")},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "req.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            proc = self._run(str(path), "--fix")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("warn", proc.stdout)
        self.assertNotIn("fixed J", proc.stderr)

    def test_watch_emits_ticks(self) -> None:
        import os as _os

        request = {
            "state": {"task": "x"},
            "questions": {"q": noul("Is it not true?")},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "req.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            env = dict(_os.environ, JEV_QLINT_WATCH_MAX="2")
            proc = subprocess.run(
                [sys.executable, str(self.QLINT_PATH), str(path), "--watch", "0.01"],
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
        self.assertTrue(all("errors" in t for t in ticks))
        self.assertTrue(all("warnings" in t and "infos" in t for t in ticks))
        stderr_lines = [
            l for l in proc.stderr.splitlines() if l.startswith("watch tick=")
        ]
        self.assertEqual(len(stderr_lines), 2)
        self.assertIn("findings=", stderr_lines[0])
        self.assertIn("errors=", stderr_lines[0])

    def test_watch_fail_fast_breaks_on_error_tick(self) -> None:
        import os as _os

        bad = {
            "state": {"task": "x"},
            "questions": {"q": noul("Is it not true? Isn't it wrong?")},
        }
        with tempfile.TemporaryDirectory() as tmp:
            badp = Path(tmp) / "bad.json"
            badp.write_text(json.dumps(bad), encoding="utf-8")
            env = dict(_os.environ, JEV_QLINT_WATCH_MAX="5")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(self.QLINT_PATH),
                    str(badp),
                    "--watch",
                    "0.01",
                    "--fail-fast",
                ],
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

    def test_watch_rc_reflects_last_lint(self) -> None:
        import os as _os

        bad = {
            "state": {"task": "x"},
            "questions": {"q": noul("Is it not true? Isn't it wrong?")},
        }
        good = {"state": {"task": "x"}, "questions": {"q": noul("Pick an approach.")}}
        with tempfile.TemporaryDirectory() as tmp:
            badp = Path(tmp) / "bad.json"
            badp.write_text(json.dumps(bad), encoding="utf-8")
            env = dict(_os.environ, JEV_QLINT_WATCH_MAX="1")
            proc = subprocess.run(
                [sys.executable, str(self.QLINT_PATH), str(badp), "--watch", "0.01"],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                env=env,
            )
            self.assertEqual(proc.returncode, 1)
            goodp = Path(tmp) / "good.json"
            goodp.write_text(json.dumps(good), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(self.QLINT_PATH), str(goodp), "--watch", "0.01"],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                env=env,
            )
            self.assertEqual(proc.returncode, 0)

    def test_watch_verdict_writes_final_state(self) -> None:
        import os as _os

        request = {"state": {"task": "x"}, "questions": {"q": noul("Pick one.")}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "req.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            verdict = Path(tmp) / "v.json"
            env = dict(_os.environ, JEV_QLINT_WATCH_MAX="2")
            proc = subprocess.run(
                [
                    sys.executable, str(self.QLINT_PATH), str(path),
                    "--watch", "0.01", "--verdict", str(verdict),
                ],
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

    def test_nonwatch_verdict_writes_single_shot(self) -> None:
        request = {"state": {"task": "x"}, "questions": {"q": noul("Pick one.")}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "req.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            verdict = Path(tmp) / "v.json"
            proc = subprocess.run(
                [
                    sys.executable, str(self.QLINT_PATH), str(path),
                    "--verdict", str(verdict),
                ],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "pass")
            self.assertEqual(payload["ticks"], 1)
            self.assertEqual(payload["errors"], 0)

    def test_watch_appends_ticks_to_out_file(self) -> None:
        import os as _os

        request = {"state": {"task": "x"}, "questions": {"q": noul("Pick one.")}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "req.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            out = Path(tmp) / "ticks.jsonl"
            env = dict(_os.environ, JEV_QLINT_WATCH_MAX="2")
            proc = subprocess.run(
                [
                    sys.executable, str(self.QLINT_PATH), str(path),
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


class LintStateEdgeTests(unittest.TestCase):
    def test_dict_state_over_limit_is_j020(self) -> None:
        request = {
            "state": {"blob": "1" * 100000},
            "questions": {"q": noul("Should the coder proceed with the plan?")},
        }
        findings = question_lint.lint_request(request)
        self.assertIn(("J020", "error"), rules(findings))

    def test_missing_state_no_state_finding(self) -> None:
        request = {"questions": {"q": noul("Should the coder proceed with the plan?")}}
        findings = question_lint.lint_request(request)
        self.assertNotIn(("J020", "error"), rules(findings))
        self.assertNotIn(("J021", "warn"), rules(findings))

    def test_none_state_is_tiny(self) -> None:
        request = {
            "state": None,
            "questions": {"q": noul("Should the coder proceed with the plan?")},
        }
        findings = question_lint.lint_request(request)
        self.assertNotIn(("J020", "error"), rules(findings))

    def test_list_state_serializes(self) -> None:
        request = {
            "state": ["1" * 50000, "1" * 60000],
            "questions": {"q": noul("Should the coder proceed with the plan?")},
        }
        findings = question_lint.lint_request(request)
        self.assertIn(("J020", "error"), rules(findings))


class ApplyFixesTests(unittest.TestCase):
    def test_no_questions_dict(self) -> None:
        self.assertEqual(question_lint.apply_fixes({}), [])
        self.assertEqual(question_lint.apply_fixes({"questions": "nope"}), [])

    def test_skips_non_dict_question(self) -> None:
        request = {"questions": {"bad": "not-a-dict", "ok": noul("fine?")}}
        self.assertEqual(question_lint.apply_fixes(request), [])

    def test_j009_fills_only_empty_entries(self) -> None:
        request = {
            "questions": {
                "pick": {
                    "type": "choice",
                    "instructions": "Which library should handle the JWT signing?",
                    "criteria": {"jwtlib": "", "other": "", "mine": "keep"},
                }
            }
        }
        applied = question_lint.apply_fixes(request)
        self.assertEqual(applied, ["J009"])
        criteria = request["questions"]["pick"]["criteria"]
        self.assertEqual(criteria["jwtlib"], "jwtlib")
        self.assertEqual(criteria["other"], "other")
        self.assertEqual(criteria["mine"], "keep")

    def test_explain_prints_rule_description(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = question_lint.main(["--explain", "J012"])
        self.assertEqual(rc, 0)
        self.assertIn("J012:", buf.getvalue())
        self.assertIn("overlap", buf.getvalue())

    def test_explain_unknown_rule_rc2(self) -> None:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = question_lint.main(["--explain", "J999"])
        self.assertEqual(rc, 2)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = question_lint.main(["--explain"])
        self.assertEqual(rc, 2)

    def test_rules_lists_every_rule_sorted(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = question_lint.main(["--rules"])
        self.assertEqual(rc, 0)
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), len(question_lint.RULES))
        self.assertIn("J001:", lines[0])

    def test_rules_json_shape(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = question_lint.main(["--rules", "--json"])
        self.assertEqual(rc, 0)
        rows = json.loads(buf.getvalue())
        self.assertEqual(
            sorted(r["rule"] for r in rows), sorted(question_lint.RULES)
        )
        self.assertTrue(all(r["description"] for r in rows))


class WatchJqTests(unittest.TestCase):
    def test_watch_jq_prints_only_named_tick_field(self) -> None:
        import os as _os

        request = {
            "state": {"task": "x"},
            "questions": {"q": noul("Should the coder proceed with the plan?")},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "req.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            buf = io.StringIO()
            with patch.dict(_os.environ, {"JEV_QLINT_WATCH_MAX": "2"}):
                with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                    rc = question_lint.main(
                        [str(path), "--watch", "0.01", "--jq", "errors"]
                    )
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().splitlines(), ["0", "0"])

class WatchSecsEnvTests(unittest.TestCase):
    def test_watch_secs_env_bounds_loop(self) -> None:
        import os as _os
        import time as _time

        request = {
            "state": {"task": "x"},
            "questions": {"q": noul("Is it not true?")},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "req.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            env = dict(
                _os.environ,
                JEV_QLINT_WATCH_MAX="0",
                JEV_QLINT_WATCH_SECS="0.05",
            )
            start = _time.time()
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "question_lint.py"), str(path), "--watch", "0.02"],
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

    def test_choice_without_none_warns_j016(self) -> None:
        findings = question_lint.lint_question(
            "q",
            {
                "type": "choice",
                "instructions": "Which of these should the coder use?",
                "criteria": {"a": "option a", "b": "option b"},
            },
        )
        self.assertIn(("J016", "warn"), rules(findings))

    def test_choice_with_none_is_clean_j016(self) -> None:
        findings = question_lint.lint_question(
            "q",
            {
                "type": "choice",
                "instructions": "Which of these should the coder use?",
                "criteria": {"a": "option a", "none": "none of these"},
            },
        )
        self.assertNotIn(("J016", "warn"), rules(findings))

if __name__ == "__main__":
    sys.exit(0 if unittest.main(verbosity=2) else 1)

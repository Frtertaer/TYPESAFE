#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
POLICY_PATH = ROOT / "skills" / "jev-consult" / "policy.json"
LINT_PATH = SCRIPTS / "policy_lint.py"

sys.path.insert(0, str(SCRIPTS))

import policy_lint  # noqa: E402


def base_policy() -> dict:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def rule_ids(findings):
    return {f["rule"] for f in findings}


def errors(findings):
    return [f for f in findings if f["severity"] == "error"]


class PolicyLintTests(unittest.TestCase):
    def test_real_policy_has_no_errors(self) -> None:
        findings = policy_lint.lint_policy(base_policy())
        self.assertEqual(errors(findings), [])

    def test_non_object_policy(self) -> None:
        findings = policy_lint.lint_policy([1, 2, 3])
        self.assertIn("P000", rule_ids(findings))
        self.assertTrue(errors(findings))

    def test_missing_required_key(self) -> None:
        policy = base_policy()
        del policy["confidence_floor"]
        self.assertIn("P001", rule_ids(policy_lint.lint_policy(policy)))

    def test_threshold_out_of_range(self) -> None:
        policy = base_policy()
        policy["noul_yes"] = 1.7
        findings = policy_lint.lint_policy(policy)
        self.assertIn("P002", rule_ids(findings))
        self.assertTrue(errors(findings))
        policy = base_policy()
        policy["noul_yes"] = "high"
        self.assertIn("P002", rule_ids(policy_lint.lint_policy(policy)))

    def test_bool_field_rejects_non_bool(self) -> None:
        policy = base_policy()
        policy["escalate_if"]["irreversible"] = "yes"
        self.assertIn("P002", rule_ids(policy_lint.lint_policy(policy)))
        policy = base_policy()
        policy["require_hatch"] = 1
        self.assertIn("P002", rule_ids(policy_lint.lint_policy(policy)))

    def test_unknown_escalate_key_warns(self) -> None:
        policy = base_policy()
        policy["escalate_if"]["confidene_below"] = 0.4
        findings = policy_lint.lint_policy(policy)
        self.assertIn("P010", rule_ids(findings))
        self.assertFalse(errors(findings))

    def test_unknown_top_level_key_warns(self) -> None:
        policy = base_policy()
        policy["noul_yess"] = 0.8
        findings = policy_lint.lint_policy(policy)
        self.assertIn("P011", rule_ids(findings))
        self.assertFalse(errors(findings))

    def test_real_policy_has_no_unknown_keys(self) -> None:
        findings = policy_lint.lint_policy(base_policy())
        self.assertNotIn("P011", rule_ids(findings))

    def test_endpoint_must_be_https(self) -> None:
        policy = base_policy()
        policy["endpoint"] = "http://api.typesafe.ai/v1/systemone"
        findings = policy_lint.lint_policy(policy)
        self.assertIn("P011", rule_ids(findings))
        self.assertTrue(errors(findings))

    def test_noul_band_ordering(self) -> None:
        policy = base_policy()
        policy["noul_no"] = 0.9
        findings = policy_lint.lint_policy(policy)
        self.assertIn("P004", rule_ids(findings))
        self.assertTrue(errors(findings))

    def test_strong_pick_must_clear_floor(self) -> None:
        policy = base_policy()
        policy["strong_pick"] = 0.4
        self.assertIn("P004", rule_ids(policy_lint.lint_policy(policy)))

    def test_soft_hard_ordering(self) -> None:
        policy = base_policy()
        policy["question_soft_max"] = 64
        self.assertIn("P004", rule_ids(policy_lint.lint_policy(policy)))

    def test_noul_near_inside_bands(self) -> None:
        policy = base_policy()
        policy["escalate_if"]["noul_near"] = 0.9
        self.assertIn("P004", rule_ids(policy_lint.lint_policy(policy)))

    def test_mirrored_threshold_drift_warns(self) -> None:
        policy = base_policy()
        policy["escalate_if"]["confidence_below"] = 0.9
        findings = policy_lint.lint_policy(policy)
        self.assertIn("P005", rule_ids(findings))
        self.assertFalse(errors(findings))

    def test_must_ask_never_ask_overlap(self) -> None:
        policy = base_policy()
        policy["never_ask"] = policy["never_ask"] + ["approach"]
        findings = policy_lint.lint_policy(policy)
        self.assertIn("P006", rule_ids(findings))
        self.assertTrue(errors(findings))

    def test_must_ask_kind_needs_template(self) -> None:
        policy = base_policy()
        del policy["templates"]["library"]
        self.assertIn("P012", rule_ids(policy_lint.lint_policy(policy)))

    def test_template_type_checked(self) -> None:
        policy = base_policy()
        policy["templates"]["approach"]["type"] = "boolean"
        findings = policy_lint.lint_policy(policy)
        self.assertIn("P007", rule_ids(findings))
        self.assertTrue(errors(findings))

    def test_template_needs_instructions(self) -> None:
        policy = base_policy()
        policy["templates"]["approach"]["instructions"] = ""
        self.assertIn("P007", rule_ids(policy_lint.lint_policy(policy)))

    def test_instructions_should_end_with_question_mark(self) -> None:
        policy = base_policy()
        policy["templates"]["approach"]["instructions"] = "Choose an approach"
        findings = policy_lint.lint_policy(policy)
        self.assertIn("P008", rule_ids(findings))
        self.assertFalse(errors(findings))

    def test_single_nonhatch_option_errors_but_lone_hatch_ok(self) -> None:
        policy = base_policy()
        policy["templates"]["approach"]["criteria"] = {"only_way": "do it"}
        self.assertIn("P009", rule_ids(policy_lint.lint_policy(policy)))
        policy = base_policy()
        policy["templates"]["approach"]["criteria"] = {"none": "none of these"}
        self.assertNotIn("P009", rule_ids(policy_lint.lint_policy(policy)))

    def test_bad_option_id_and_empty_meaning(self) -> None:
        policy = base_policy()
        policy["templates"]["approach"]["criteria"] = {
            "Two Words": "bad id",
            "blank": " ",
            "none": "none of these",
        }
        findings = policy_lint.lint_policy(policy)
        self.assertIn("P009", rule_ids(findings))

    def test_choice_without_hatch_when_required(self) -> None:
        policy = base_policy()
        policy["templates"]["keep_vs_change"]["criteria"] = {
            "keep": "leave it",
            "change": "edit it",
        }
        findings = policy_lint.lint_policy(policy)
        self.assertIn("P009", rule_ids(findings))
        self.assertTrue(errors(findings))

    def test_noul_with_criteria_warns(self) -> None:
        policy = base_policy()
        policy["templates"]["delete"]["criteria"] = {"yes": "delete", "no": "keep"}
        findings = policy_lint.lint_policy(policy)
        self.assertIn("P009", rule_ids(findings))
        self.assertFalse(errors(findings))

    def test_duplicate_instructions_warn(self) -> None:
        policy = base_policy()
        policy["templates"]["library"]["instructions"] = policy["templates"]["approach"]["instructions"]
        findings = policy_lint.lint_policy(policy)
        self.assertIn("P008", rule_ids(findings))
        self.assertFalse(errors(findings))

    def test_cli_exit_codes(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(LINT_PATH)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "policy.json"
            bad.write_text("{not json", encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(LINT_PATH), str(bad)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 2)
            policy = base_policy()
            policy["noul_yes"] = 1.7
            bad.write_text(json.dumps(policy), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(LINT_PATH), str(bad)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 1)

    def test_strict_fails_on_warnings(self) -> None:
        policy = base_policy()
        policy["escalate_if"]["confidence_below"] = 0.9
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "policy.json"
            p.write_text(json.dumps(policy), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(LINT_PATH), str(p)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0)
            proc = subprocess.run(
                [sys.executable, str(LINT_PATH), str(p), "--strict"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 1)

    def test_jev_ask_surfaces_policy_warnings(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("jev", SCRIPTS / "jev.py")
        jev = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules["jev"] = jev
        spec.loader.exec_module(jev)
        policy = base_policy()
        policy["escalate_if"]["choice_gap_below"] = 0.77
        warnings = jev.policy_warnings(policy)
        self.assertTrue(any("policy_lint P005" in w for w in warnings))

    def test_main_defaults_to_pack_policy(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = policy_lint.main([])
        self.assertEqual(rc, 0)
        self.assertIn("0 error(s)", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

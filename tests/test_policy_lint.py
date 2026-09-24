#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
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

    def test_real_policy_has_no_warnings(self) -> None:
        findings = policy_lint.lint_policy(base_policy())
        warns = [f for f in findings if f["severity"] == "warn"]
        self.assertEqual(warns, [])
        # info-level reminders (P013) are fine; anything else must not drift in
        self.assertEqual(rule_ids(findings), {"P013"})

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

    def test_endpoint_scheme_case_insensitive(self) -> None:
        policy = base_policy()
        policy["endpoint"] = "HTTPS://api.typesafe.ai/v1/systemone"
        findings = policy_lint.lint_policy(policy)
        self.assertNotIn("P011", rule_ids(findings))

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

    def test_closed_list_without_hatch_warns_when_require_hatch_off(self) -> None:
        policy = base_policy()
        policy["require_hatch"] = False
        policy["templates"]["keep_vs_change"]["criteria"] = {
            "keep": "leave it",
            "change": "edit it",
        }
        findings = policy_lint.lint_policy(policy)
        self.assertIn("P014", rule_ids(findings))
        self.assertFalse(errors(findings))

    def test_closed_list_without_hatch_clean_when_require_hatch_off_and_hatch_present(self) -> None:
        policy = base_policy()
        policy["require_hatch"] = False
        self.assertNotIn("P014", rule_ids(policy_lint.lint_policy(policy)))

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

    def test_multiple_paths_lint_each_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.json"
            good.write_text(json.dumps(base_policy()), encoding="utf-8")
            badpol = base_policy()
            badpol["noul_yes"] = 1.7
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps(badpol), encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = policy_lint.main([str(good), str(bad)])
            self.assertEqual(rc, 1)
            out = buf.getvalue()
            self.assertIn(str(good) + ":", out)
            self.assertIn(str(bad) + ":", out)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = policy_lint.main([str(good), str(good)])
            self.assertEqual(rc, 0)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = policy_lint.main([str(good), str(bad), "--json"])
            self.assertEqual(rc, 1)
            rows = json.loads(buf.getvalue())
            self.assertEqual(len(rows), 2)
            self.assertTrue(rows[0]["path"].endswith("good.json"))
            self.assertEqual(rows[1]["errors"], 1)
            proc = subprocess.run(
                [sys.executable, str(LINT_PATH), str(good), str(good)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("good.json:", proc.stdout)

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

    def test_progress_invalid_limit_reaches_policy_lint(self):
        candidate = base_policy()
        candidate["progress"]["review_points"] = 0
        self.assertIn("P014", rule_ids(policy_lint.lint_policy(candidate)))

    def test_progress_category_map_must_match_rubric(self):
        candidate = base_policy()
        candidate["progress"]["points"].pop("material")
        self.assertIn("P014", rule_ids(policy_lint.lint_policy(candidate)))

    def test_main_defaults_to_pack_policy(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = policy_lint.main([])
        self.assertEqual(rc, 0)
        self.assertIn("0 error(s)", buf.getvalue())

    def test_self_test_finds_p001(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = policy_lint.main(["--self-test"])
        self.assertEqual(rc, 0)
        self.assertIn("self-test: ok", buf.getvalue())
        self.assertIn("P001", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = policy_lint.main(["--self-test", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["self_test"], "ok")
        self.assertIn("P001", payload["rules"])

    def test_quiet_suppresses_warn_lines(self) -> None:
        policy = base_policy()
        policy["escalate_if"]["confidene_below"] = 0.4
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "policy.json"
            p.write_text(json.dumps(policy), encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = policy_lint.main([str(p), "--quiet"])
        self.assertEqual(rc, 0)
        self.assertNotIn("P010", buf.getvalue())
        self.assertNotIn("policy_lint:", buf.getvalue())

    def test_out_writes_findings_json(self) -> None:
        policy = base_policy()
        policy["escalate_if"]["confidene_below"] = 0.4
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "policy.json"
            p.write_text(json.dumps(policy), encoding="utf-8")
            out_path = Path(tmp) / "findings.json"
            err = io.StringIO()
            from contextlib import redirect_stderr

            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = policy_lint.main([str(p), "--out", str(out_path)])
            self.assertEqual(rc, 0)
            self.assertIn("wrote", err.getvalue())
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertIn("findings", payload)
            self.assertTrue(any(f["rule"] == "P010" for f in payload["findings"]))
            with redirect_stderr(err := io.StringIO()):
                rc = policy_lint.main([str(p), "--out"])
            self.assertEqual(rc, 2)

    def test_severity_filters_lines(self) -> None:
        policy = base_policy()
        policy["escalate_if"]["confidene_below"] = 0.4
        policy["noul_yes"] = 1.7
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "policy.json"
            p.write_text(json.dumps(policy), encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = policy_lint.main([str(p), "--severity", "warn"])
            self.assertEqual(rc, 1)  # error still sets rc
            self.assertIn("P010", buf.getvalue())
            self.assertNotIn("P002", buf.getvalue().split("policy_lint:")[0])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = policy_lint.main([str(p), "--severity", "bogus"])
            self.assertEqual(rc, 2)

    def test_severity_comma_list(self) -> None:
        policy = base_policy()
        policy["escalate_if"]["confidene_below"] = 0.4
        policy["noul_yes"] = 1.7
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "policy.json"
            p.write_text(json.dumps(policy), encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = policy_lint.main([str(p), "--severity", "warn,error"])
            self.assertEqual(rc, 1)
            head = buf.getvalue().split("policy_lint:")[0]
            self.assertIn("P010", head)
            self.assertIn("P002", head)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = policy_lint.main([str(p), "--severity", "warn,bogus"])
            self.assertEqual(rc, 2)

    def test_severity_env_default(self) -> None:
        import os
        from unittest import mock

        policy = base_policy()
        policy["escalate_if"]["confidene_below"] = 0.4
        policy["noul_yes"] = 1.7
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "policy.json"
            p.write_text(json.dumps(policy), encoding="utf-8")
            with mock.patch.dict(os.environ, {"JEV_PLINT_SEVERITY": "warn"}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = policy_lint.main([str(p)])
            self.assertEqual(rc, 1)
            head = buf.getvalue().split("policy_lint:")[0]
            self.assertIn("P010", head)
            self.assertNotIn("P002", head)
            with mock.patch.dict(os.environ, {"JEV_PLINT_SEVERITY": "warn"}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    policy_lint.main([str(p), "--severity", "error"])
            self.assertIn("P002", buf.getvalue())

    def test_json_emits_machine_readable(self) -> None:
        policy = base_policy()
        policy["escalate_if"]["confidene_below"] = 0.4
        policy["noul_yes"] = 1.7
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "policy.json"
            p.write_text(json.dumps(policy), encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = policy_lint.main([str(p), "--json"])
            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                policy_lint.main([str(p), "--json", "--severity", "warn"])
        self.assertEqual(rc, 1)
        payload = json.loads(buf.getvalue())
        rules = {f["rule"] for f in payload["findings"]}
        self.assertIn("P002", rules)
        self.assertIn("P010", rules)
        self.assertEqual(payload["errors"], 1)
        self.assertGreaterEqual(payload["warnings"], 1)
        payload = json.loads(buf2.getvalue())
        self.assertTrue(all(f["severity"] == "warn" for f in payload["findings"]))
        self.assertEqual(payload["errors"], 1)  # counts still on all findings

    def test_quiet_still_prints_errors(self) -> None:
        policy = base_policy()
        policy["noul_yes"] = 1.7
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "policy.json"
            p.write_text(json.dumps(policy), encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = policy_lint.main([str(p), "--quiet"])
        self.assertEqual(rc, 1)
        self.assertIn("P002", buf.getvalue())


class UsageScanTests(unittest.TestCase):
    def test_helper_flags_dead_known_key(self) -> None:
        findings = policy_lint.usage_findings({"noul_unsure": 0.5})
        self.assertEqual([f["path"] for f in findings], ["noul_unsure"])
        self.assertEqual(findings[0]["rule"], "P015")
        self.assertEqual(findings[0]["severity"], "info")

    def test_helper_skips_unknown_and_live_keys(self) -> None:
        findings = policy_lint.usage_findings({"zzz_unknown": 1, "model": "m"})
        self.assertEqual(findings, [])

    def test_helper_scan_dir_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "reader.py").write_text(
                'x = policy["noul_unsure"]\n', encoding="utf-8"
            )
            findings = policy_lint.usage_findings(
                {"noul_unsure": 0.5}, scripts_dir=Path(tmp)
            )
        self.assertEqual(findings, [])

    def test_cli_usage_flags_dead_keys_on_real_policy(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = policy_lint.main([str(POLICY_PATH), "--usage", "--json"])
        self.assertEqual(rc, 0)  # info findings never fail the run
        payload = json.loads(buf.getvalue())
        dead = {f["path"] for f in payload["findings"] if f["rule"] == "P015"}
        self.assertEqual(dead, {"coder_role", "hallucination", "never_ask", "noul_unsure"})

    def test_cli_without_usage_has_no_p015(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = policy_lint.main([str(POLICY_PATH), "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertNotIn("P015", rule_ids(payload["findings"]))


class ShowFlagTests(unittest.TestCase):
    def test_show_prints_resolved_policy(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = policy_lint.main(["--show"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertIn("policy", data)
        self.assertIn("confidence_floor", data["policy"])

    def test_show_bad_path_rc2(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = policy_lint.main(["--show", "no-such-policy.json"])
        self.assertEqual(rc, 2)


class DiffFlagTests(unittest.TestCase):
    def _write(self, tmp: str, data: dict) -> Path:
        path = Path(tmp) / "other.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_diff_identical_zero(self) -> None:
        policy = json.loads(policy_lint.DEFAULT_POLICY.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            other = self._write(tmp, policy)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = policy_lint.main(["--diff", str(other)])
        self.assertEqual(rc, 0)
        self.assertIn("0 difference(s)", buf.getvalue())

    def test_diff_reports_add_remove_change(self) -> None:
        policy = json.loads(policy_lint.DEFAULT_POLICY.read_text(encoding="utf-8"))
        other = dict(policy)
        other.pop("model")
        other["brand_new"] = 1
        other["confidence_floor"] = 0.99
        with tempfile.TemporaryDirectory() as tmp:
            other_path = self._write(tmp, other)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = policy_lint.main(["--diff", str(other_path)])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("- brand_new = 1", out)
        self.assertIn("+ model =", out)
        self.assertIn("~ confidence_floor: 0.99 -> 0.55", out)

    def test_diff_bad_file_rc2(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = policy_lint.main(["--diff", "no-such.json"])
        self.assertEqual(rc, 2)

    def test_diff_missing_arg_rc2(self) -> None:
        rc = policy_lint.main(["--diff"])
        self.assertEqual(rc, 2)

    def test_diff_non_object_rc2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp) / "list.json"
            other.write_text("[1, 2]", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = policy_lint.main(["--diff", str(other)])
        self.assertEqual(rc, 2)
        self.assertIn("JSON objects", buf.getvalue())

    def test_diff_dash_reads_other_from_stdin(self) -> None:
        policy = json.loads(policy_lint.DEFAULT_POLICY.read_text(encoding="utf-8"))
        other = dict(policy)
        other.pop("model")
        from unittest.mock import patch

        buf = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(other))):
            with redirect_stdout(buf):
                rc = policy_lint.main(["--diff", "-"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("diff - ->", out)
        self.assertIn("+ model =", out)

    def test_diff_dash_bad_json_rc2(self) -> None:
        from unittest.mock import patch

        buf = io.StringIO()
        with patch("sys.stdin", io.StringIO("{bad")):
            with redirect_stdout(buf):
                rc = policy_lint.main(["--diff", "-"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot parse -", buf.getvalue())


class WatchFlagTests(unittest.TestCase):
    def test_watch_emits_ticks(self) -> None:
        import os
        from unittest import mock

        buf = io.StringIO()
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"JEV_PLINT_WATCH_MAX": "2"}):
            with redirect_stdout(buf), redirect_stderr(err):
                rc = policy_lint.main(["--watch", "0.01"])
        self.assertEqual(rc, 0)
        ticks = [
            json.loads(l) for l in buf.getvalue().splitlines() if l.startswith("{")
        ]
        self.assertEqual(len(ticks), 2)
        self.assertTrue(all("errors" in t for t in ticks))
        self.assertTrue(all("warnings" in t and "infos" in t for t in ticks))
        stderr_lines = [
            l for l in err.getvalue().splitlines() if l.startswith("watch tick=")
        ]
        self.assertEqual(len(stderr_lines), 2)
        self.assertIn("findings=", stderr_lines[0])
        self.assertIn("errors=", stderr_lines[0])

    def test_watch_bad_value_rc2(self) -> None:
        with redirect_stdout(io.StringIO()):
            rc = policy_lint.main(["--watch", "bogus"])
        self.assertEqual(rc, 2)

    def test_watch_rc_reflects_last_lint(self) -> None:
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text('{"escalate_if": "x"}', encoding="utf-8")
            buf = io.StringIO()
            with mock.patch.dict(os.environ, {"JEV_PLINT_WATCH_MAX": "1"}):
                with redirect_stdout(buf):
                    rc = policy_lint.main([str(bad), "--watch", "0.01"])
            self.assertEqual(rc, 1)
            with mock.patch.dict(os.environ, {"JEV_PLINT_WATCH_MAX": "1"}):
                with redirect_stdout(io.StringIO()):
                    rc = policy_lint.main(["--watch", "0.01"])
            self.assertEqual(rc, 0)

    def test_watch_appends_ticks_to_out_file(self) -> None:
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ticks.jsonl"
            with mock.patch.dict(os.environ, {"JEV_PLINT_WATCH_MAX": "2"}):
                with redirect_stdout(io.StringIO()):
                    rc = policy_lint.main(
                        ["--watch", "0.01", "--out", str(out)]
                    )
            self.assertEqual(rc, 0)
            lines = [
                json.loads(l)
                for l in out.read_text(encoding="utf-8").splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(lines), 2)
            self.assertTrue(all("findings" in t and "errors" in t for t in lines))

    def test_watch_fail_fast_breaks_on_error_tick(self) -> None:
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text('{"escalate_if": "x"}', encoding="utf-8")
            buf = io.StringIO()
            with mock.patch.dict(os.environ, {"JEV_PLINT_WATCH_MAX": "5"}):
                with redirect_stdout(buf):
                    rc = policy_lint.main(
                        [str(bad), "--watch", "0.01", "--fail-fast"]
                    )
            self.assertEqual(rc, 1)
            ticks = [
                json.loads(l)
                for l in buf.getvalue().splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 1)

    def test_watch_max_removes_right_argv_pair(self) -> None:
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text('{"escalate_if": "x"}', encoding="utf-8")
            with mock.patch.dict(os.environ, {"JEV_PLINT_WATCH_MAX": "1"}):
                with redirect_stdout(io.StringIO()):
                    rc = policy_lint.main(
                        [str(bad), "--watch", "0.01", "--watch-max", "5"]
                    )
            self.assertEqual(rc, 1)

    def test_watch_verdict_writes_final_state(self) -> None:
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text('{"escalate_if": "x"}', encoding="utf-8")
            verdict = Path(tmp) / "v.json"
            with mock.patch.dict(os.environ, {"JEV_PLINT_WATCH_MAX": "1"}):
                with redirect_stdout(io.StringIO()):
                    rc = policy_lint.main(
                        [str(bad), "--watch", "0.01", "--verdict", str(verdict)]
                    )
            self.assertEqual(rc, 1)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "fail")
            self.assertEqual(payload["ticks"], 1)
            self.assertGreater(payload["errors"], 0)
            self.assertIn("findings", payload)

    def test_watch_verdict_pass_on_clean_file(self) -> None:
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            verdict = Path(tmp) / "v.json"
            with mock.patch.dict(os.environ, {"JEV_PLINT_WATCH_MAX": "1"}):
                with redirect_stdout(io.StringIO()):
                    rc = policy_lint.main(
                        ["--watch", "0.01", "--verdict", str(verdict)]
                    )
            self.assertEqual(rc, 0)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "pass")

    def test_nonwatch_verdict_writes_single_shot(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            verdict = Path(tmp) / "v.json"
            with redirect_stdout(io.StringIO()):
                rc = policy_lint.main(["--verdict", str(verdict)])
            self.assertIn(rc, (0, 1))
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertIn(payload["verdict"], ("pass", "fail"))
            self.assertEqual(payload["ticks"], 1)
            self.assertIn("errors", payload)
            self.assertIn("warnings", payload)

    def test_fix_drops_unknown_keys(self) -> None:
        policy = base_policy()
        policy["typo_key"] = 1
        policy["escalate_if"]["typo_esc"] = 0.5
        applied = policy_lint.fix_policy(policy)
        self.assertEqual(applied, ["P011", "P010"])
        self.assertNotIn("typo_key", policy)
        self.assertNotIn("typo_esc", policy["escalate_if"])
        self.assertEqual(policy_lint.fix_policy({}), [])

    def test_fix_flag_rewrites_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            policy = base_policy()
            policy["typo_key"] = 1
            path.write_text(json.dumps(policy), encoding="utf-8")
            err = io.StringIO()
            with patch.object(sys, "stderr", err):
                with redirect_stdout(io.StringIO()):
                    rc = policy_lint.main([str(path), "--fix"])
            self.assertEqual(rc, 0)
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("typo_key", written)
            self.assertIn("fixed P011 x1", err.getvalue())

    def test_fix_dry_run_leaves_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            policy = base_policy()
            policy["typo_key"] = 1
            path.write_text(json.dumps(policy), encoding="utf-8")
            err = io.StringIO()
            with patch.object(sys, "stderr", err):
                with redirect_stdout(io.StringIO()):
                    rc = policy_lint.main([str(path), "--fix", "--dry-run"])
            self.assertEqual(rc, 0)
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("typo_key", written)
            self.assertIn("would fix P011 x1", err.getvalue())

    def test_fix_multi_path_rc2(self) -> None:
        with patch.object(sys, "stderr", io.StringIO()):
            rc = policy_lint.main(["a.json", "b.json", "--fix"])
        self.assertEqual(rc, 2)

    def test_explain_prints_rule_description(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = policy_lint.main(["--explain", "P004"])
        self.assertEqual(rc, 0)
        self.assertIn("P004:", buf.getvalue())
        self.assertIn("ordering", buf.getvalue())

    def test_explain_unknown_rule_rc2(self) -> None:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = policy_lint.main(["--explain", "P999"])
        self.assertEqual(rc, 2)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = policy_lint.main(["--explain"])
        self.assertEqual(rc, 2)


class RulesCatalogTest(unittest.TestCase):
    def test_rules_lists_every_rule_sorted(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = policy_lint.main(["--rules"])
        self.assertEqual(rc, 0)
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), len(policy_lint.RULES))
        self.assertIn("P001:", lines[1])
        self.assertIn("missing required key", buf.getvalue())

    def test_rules_json_shape(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = policy_lint.main(["--rules", "--json"])
        self.assertEqual(rc, 0)
        rows = json.loads(buf.getvalue())
        self.assertEqual(
            sorted(r["rule"] for r in rows), sorted(policy_lint.RULES)
        )
        self.assertTrue(all(r["description"] for r in rows))

    def test_schema_covers_known_keys(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = policy_lint.main(["--schema", "--json"])
        self.assertEqual(rc, 0)
        rows = json.loads(buf.getvalue())
        self.assertEqual(sorted(rows), sorted(policy_lint.KNOWN_TOP_KEYS))
        self.assertEqual(rows["confidence_floor"]["type"], "prob [0,1]")
        self.assertTrue(rows["confidence_floor"]["required"])
        self.assertFalse(rows["catalogs"]["required"])
        self.assertEqual(rows["escalate_if"]["type"].split("{")[0], "object")

    def test_schema_text_lists_required_marker(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = policy_lint.main(["--schema"])
        self.assertEqual(rc, 0)
        self.assertIn("confidence_floor: prob [0,1] (required)", buf.getvalue())
        self.assertIn("catalogs: list[{name,url}] (optional)", buf.getvalue())


class WatchJqTests(unittest.TestCase):
    def test_watch_jq_prints_only_named_tick_field(self) -> None:
        import os
        from unittest import mock

        buf = io.StringIO()
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"JEV_PLINT_WATCH_MAX": "2"}):
            with redirect_stdout(buf), redirect_stderr(err):
                rc = policy_lint.main(["--watch", "0.01", "--jq", "errors"])
        self.assertEqual(rc, 0)
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(l.isdigit() for l in lines))

    def test_watch_jq_missing_value_rc2(self) -> None:
        with redirect_stderr(io.StringIO()):
            rc = policy_lint.main(["--watch", "0.01", "--jq"])
        self.assertEqual(rc, 2)

class WatchQuietEnvTests(unittest.TestCase):
    def test_watch_quiet_env_suppresses_clean_ticks_but_out_logs(self) -> None:
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ticks.jsonl"
            buf = io.StringIO()
            err = io.StringIO()
            with mock.patch.dict(
                os.environ,
                {"JEV_PLINT_WATCH_MAX": "2", "JEV_PLINT_WATCH_QUIET": "1"},
            ):
                with redirect_stdout(buf), redirect_stderr(err):
                    rc = policy_lint.main(
                        ["--watch", "0.01", "--out", str(out)]
                    )
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue(), "")
            self.assertEqual(len(out.read_text(encoding="utf-8").splitlines()), 2)
            stderr_lines = [
                l for l in err.getvalue().splitlines() if l.startswith("watch tick=")
            ]
            self.assertEqual(len(stderr_lines), 2)

class WatchDeadlineEnvTests(unittest.TestCase):
    def test_watch_secs_env_bounds_loop(self) -> None:
        import os
        import time
        from unittest import mock

        buf = io.StringIO()
        err = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {"JEV_PLINT_WATCH_MAX": "0", "JEV_PLINT_WATCH_SECS": "0.05"},
        ):
            start = time.time()
            with redirect_stdout(buf), redirect_stderr(err):
                rc = policy_lint.main(["--watch", "0.02"])
        self.assertEqual(rc, 0)
        self.assertLess(time.time() - start, 2.0)
        ticks = [
            json.loads(l) for l in buf.getvalue().splitlines() if l.startswith("{")
        ]
        self.assertLessEqual(len(ticks), 10)
        self.assertGreaterEqual(len(ticks), 1)


class PolicyKeyUsageTests(unittest.TestCase):
    """Every top-level policy.json key must be referenced by name in some
    script — a key nobody reads is dead config (usually a typo)."""

    def test_every_policy_key_is_referenced(self) -> None:
        src = ""
        for p in SCRIPTS.glob("*.py"):
            src += p.read_text(encoding="utf-8")
        installer = ROOT / "scripts" / "install.py"
        if installer.is_file():
            src += installer.read_text(encoding="utf-8")
        unused = [
            k
            for k in base_policy()
            if '"%s"' % k not in src and "'%s'" % k not in src
        ]
        self.assertEqual([], unused, "policy keys never read: %s" % unused)

    def test_escalate_if_subkeys_are_referenced(self) -> None:
        # escalate_if is a structured-threshold section: every sub-key must
        # be wired into a policy_get lookup, not just linted as known.
        # (templates/catalogs/hallucination carry content, not thresholds.)
        src = ""
        for p in SCRIPTS.glob("*.py"):
            src += p.read_text(encoding="utf-8")
        section = base_policy().get("escalate_if")
        self.assertIsInstance(section, dict)
        unused = [
            k
            for k in section
            if '"%s"' % k not in src and "'%s'" % k not in src
        ]
        self.assertEqual([], unused, "escalate_if sub-keys never read: %s" % unused)


class HatchIdsTests(unittest.TestCase):
    def test_custom_hatch_ids_suppress_hatch_findings(self) -> None:
        policy = base_policy()
        policy["choice"] = {"hatch_ids": ["skip"]}
        for template in policy["templates"].values():
            criteria = template.get("criteria")
            if isinstance(criteria, dict):
                for hid in ("none", "other"):
                    criteria.pop(hid, None)
                criteria["skip"] = "skip this slot"
        findings = policy_lint.lint_policy(policy)
        template_findings = [f for f in findings if f["path"].startswith("templates.")]
        self.assertEqual(
            [f["rule"] for f in template_findings if f["rule"] in ("P009", "P014")],
            [],
        )
        self.assertNotIn("P016", rule_ids(findings))

    def test_default_hatch_ids_still_flag_missing_hatch(self) -> None:
        policy = base_policy()
        for template in policy["templates"].values():
            criteria = template.get("criteria")
            if isinstance(criteria, dict) and len(criteria) >= 2:
                criteria.pop("none", None)
                criteria.pop("other", None)
        self.assertIn("P009", rule_ids(policy_lint.lint_policy(policy)))

    def test_custom_hatch_ids_removed_fires_p009(self) -> None:
        policy = base_policy()
        policy["choice"] = {"hatch_ids": ["skip"]}
        for template in policy["templates"].values():
            criteria = template.get("criteria")
            if isinstance(criteria, dict) and len(criteria) >= 2:
                criteria.pop("none", None)
                criteria.pop("other", None)
        # "skip" not present in criteria -> still flags under custom ids
        self.assertIn("P009", rule_ids(policy_lint.lint_policy(policy)))

    def test_hatch_ids_not_list_errors(self) -> None:
        policy = base_policy()
        policy["choice"] = {"hatch_ids": "none"}
        findings = policy_lint.lint_policy(policy)
        self.assertIn("P016", rule_ids(findings))

    def test_hatch_ids_empty_errors(self) -> None:
        policy = base_policy()
        policy["choice"] = {"hatch_ids": []}
        self.assertIn("P016", rule_ids(policy_lint.lint_policy(policy)))

    def test_hatch_ids_non_string_member_errors(self) -> None:
        policy = base_policy()
        policy["choice"] = {"hatch_ids": ["none", 3]}
        self.assertIn("P016", rule_ids(policy_lint.lint_policy(policy)))

    def test_hatch_ids_duplicates_error(self) -> None:
        policy = base_policy()
        policy["choice"] = {"hatch_ids": ["None", "none"]}
        self.assertIn("P016", rule_ids(policy_lint.lint_policy(policy)))

    def test_choice_section_non_object_errors(self) -> None:
        policy = base_policy()
        policy["choice"] = "yes"
        findings = policy_lint.lint_policy(policy)
        self.assertIn("P003", rule_ids(findings))

    def test_choice_key_not_unknown(self) -> None:
        policy = base_policy()
        policy["choice"] = {"hatch_ids": ["none", "other"]}
        findings = policy_lint.lint_policy(policy)
        self.assertNotIn("P011", rule_ids(findings))


class BaselineTests(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(LINT_PATH), *args],
            capture_output=True,
            text=True,
        )

    def test_baseline_suppresses_known_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = base_policy()
            policy["noul_yes"] = 1.7  # P001 error
            bad = Path(tmp) / "policy.json"
            bad.write_text(json.dumps(policy), encoding="utf-8")
            baseline = Path(tmp) / "baseline.json"
            verdict = Path(tmp) / "verdict.json"

            proc = self._run(str(bad), "--baseline-write", str(baseline))
            self.assertEqual(proc.returncode, 1, proc.stderr)
            saved = json.loads(baseline.read_text(encoding="utf-8"))
            self.assertTrue(saved["findings"])

            proc = self._run(
                str(bad),
                "--baseline",
                str(baseline),
                "--strict",
                "--verdict",
                str(verdict),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("suppressed", proc.stderr)
            data = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(data["verdict"], "pass")
            self.assertGreater(data["suppressed"], 0)

    def test_baseline_missing_file_counts_everything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = base_policy()
            policy["noul_yes"] = 1.7
            bad = Path(tmp) / "policy.json"
            bad.write_text(json.dumps(policy), encoding="utf-8")
            proc = self._run(str(bad), "--baseline", str(Path(tmp) / "nope.json"))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("not found", proc.stderr)


class StdinDashTests(unittest.TestCase):
    def _feed(self, argv, stdin_text):
        buf, err = io.StringIO(), io.StringIO()
        with patch("sys.stdin", io.StringIO(stdin_text)):
            with redirect_stdout(buf), redirect_stderr(err):
                rc = policy_lint.main(argv)
        return rc, buf.getvalue(), err.getvalue()

    def test_stdin_single(self) -> None:
        rc, out, _err = self._feed(["-", "--json"], json.dumps(base_policy()))
        payload = json.loads(out)
        self.assertEqual(payload["path"], "<stdin>")
        self.assertEqual(rc, 0)

    def test_stdin_bad_json_is_p000(self) -> None:
        rc, out, _err = self._feed(["-"], "{bad")
        self.assertIn("P000", out)
        self.assertEqual(rc, 2)

    def test_stdin_fix_and_watch_rejected(self) -> None:
        cases = [
            ["-", "--fix"],
            ["-", "--watch", "1"],
            ["-", "--diff", str(POLICY_PATH)],
        ]
        for argv in cases:
            rc, _o, err = self._feed(argv, json.dumps(base_policy()))
            self.assertEqual(rc, 2, argv)
            self.assertIn("stdin", err)

    def test_stdin_multi_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp) / "p2.json"
            other.write_text(json.dumps(base_policy()), encoding="utf-8")
            rc, out, _err = self._feed([str(other), "-", "--json"], json.dumps(base_policy()))
            rows = json.loads(out)
            self.assertEqual(rows[1]["path"], "<stdin>")
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()

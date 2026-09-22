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

    def test_multiple_paths_lint_each_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = write_cases(tmp, [dict(GOOD_CASE)])
            bad = Path(tmp) / "bad.json"
            bad.write_text(
                json.dumps({"cases": [{"id": "pos-x", "should_trigger": True}]}),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = trigger_lint.main([str(good), str(bad)])
            self.assertEqual(rc, 1)
            out = buf.getvalue()
            self.assertIn(str(good) + ":", out)
            self.assertIn(str(bad) + ":", out)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = trigger_lint.main([str(good), str(good)])
            self.assertEqual(rc, 0)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = trigger_lint.main([str(good), str(bad), "--json"])
            self.assertEqual(rc, 1)
            rows = json.loads(buf.getvalue())
            self.assertEqual(len(rows), 2)
            self.assertTrue(rows[0]["path"].endswith("cases.json"))
            self.assertGreaterEqual(rows[1]["errors"], 1)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = trigger_lint.main(
                    [str(good), str(bad), "--watch", "0.01"]
                )
            self.assertEqual(rc, 2)

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

    def test_strict_turns_warnings_into_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "policy.json"
            policy.write_text(
                json.dumps({"must_ask": ["approach", "other_kind"]}),
                encoding="utf-8",
            )
            path = write_cases(tmp, [dict(GOOD_CASE)])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = trigger_lint.main([str(path), "--policy", str(policy)])
            self.assertEqual(rc, 0)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = trigger_lint.main([str(path), "--policy", str(policy), "--strict"])
            self.assertEqual(rc, 1)

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

    def test_fix_rewrites_ids_and_covers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cases(
                tmp,
                [
                    {
                        "id": "Pick Me!",
                        "prompt": "Decide which approach to take first",
                        "should_trigger": True,
                        "covers": ["approach", "approach", 42],
                    },
                    {
                        "id": "Bad ID",
                        "prompt": "unrelated negative prompt here",
                        "should_trigger": False,
                    },
                ],
            )
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = trigger_lint.main([str(path), "--fix"])
            data = json.loads(path.read_text(encoding="utf-8"))
            ids = [c["id"] for c in data["cases"]]
            self.assertIn("pos-pick-me", ids)
            self.assertIn("neg-bad-id", ids)
            self.assertEqual(data["cases"][0]["covers"], ["approach"])
            self.assertIn("fixed T005", err.getvalue())
            self.assertIn("fixed T007", err.getvalue())
            # lint now reports no T005/T007 findings
            buf = io.StringIO()
            with redirect_stdout(buf):
                trigger_lint.main([str(path)])
            self.assertNotIn("T005", buf.getvalue())
            self.assertNotIn("T007", buf.getvalue())
            self.assertIsNotNone(rc)

    def test_fix_nothing_to_do(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cases(tmp, [dict(GOOD_CASE)])
            before = path.read_text(encoding="utf-8")
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = trigger_lint.main([str(path), "--fix"])
            self.assertEqual(rc, 0)
            self.assertEqual(path.read_text(encoding="utf-8"), before)
            self.assertNotIn("fixed", err.getvalue())

    def test_fix_dry_run_reports_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cases(
                tmp,
                [
                    {
                        "id": "Pick Me!",
                        "prompt": "Decide which approach to take first",
                        "should_trigger": True,
                        "covers": ["approach", "approach", 42],
                    },
                ],
            )
            before = path.read_text(encoding="utf-8")
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = trigger_lint.main([str(path), "--fix", "--dry-run"])
            self.assertEqual(path.read_text(encoding="utf-8"), before)
            self.assertIn("would fix", err.getvalue())
            self.assertIsNotNone(rc)

    def test_watch_emits_ticks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cases(tmp, [dict(GOOD_CASE)])
            with mock.patch.dict(os.environ, {"JEV_TLINT_WATCH_MAX": "2"}):
                buf = io.StringIO()
                err = io.StringIO()
                with redirect_stdout(buf), redirect_stderr(err):
                    rc = trigger_lint.main([str(path), "--watch", "0.01"])
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

    def test_watch_appends_ticks_to_out_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cases(tmp, [dict(GOOD_CASE)])
            out = Path(tmp) / "ticks.jsonl"
            with mock.patch.dict(os.environ, {"JEV_TLINT_WATCH_MAX": "2"}):
                with redirect_stdout(io.StringIO()):
                    rc = trigger_lint.main(
                        [str(path), "--watch", "0.01", "--out", str(out)]
                    )
            self.assertEqual(rc, 0)
            lines = [
                json.loads(l)
                for l in out.read_text(encoding="utf-8").splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(lines), 2)
            self.assertTrue(all("findings" in t and "ts" in t for t in lines))

    def test_watch_verdict_writes_final_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cases(tmp, [dict(GOOD_CASE)])
            verdict = Path(tmp) / "v.json"
            with mock.patch.dict(os.environ, {"JEV_TLINT_WATCH_MAX": "2"}):
                with redirect_stdout(io.StringIO()):
                    rc = trigger_lint.main(
                        [str(path), "--watch", "0.01", "--verdict", str(verdict)]
                    )
            self.assertEqual(rc, 0)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "pass")
            self.assertEqual(payload["ticks"], 2)
            self.assertEqual(payload["errors"], 0)

    def test_nonwatch_verdict_writes_single_shot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cases(tmp, [dict(GOOD_CASE)])
            verdict = Path(tmp) / "v.json"
            with redirect_stdout(io.StringIO()):
                rc = trigger_lint.main([str(path), "--verdict", str(verdict)])
            self.assertEqual(rc, 0)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "pass")
            self.assertEqual(payload["ticks"], 1)
            self.assertEqual(payload["errors"], 0)

    def test_watch_rc_reflects_last_lint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = write_cases(tmp, [dict(GOOD_CASE)])
            with mock.patch.dict(os.environ, {"JEV_TLINT_WATCH_MAX": "1"}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = trigger_lint.main([str(good), "--watch", "0.01"])
            self.assertEqual(rc, 0)
            bad = write_cases(
                tmp,
                [
                    {
                        "id": "Pick Me!",
                        "prompt": "Decide which approach to take first",
                        "should_trigger": True,
                        "covers": ["nope-kind"],
                    },
                ],
            )
            with mock.patch.dict(os.environ, {"JEV_TLINT_WATCH_MAX": "1"}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = trigger_lint.main([str(bad), "--watch", "0.01"])
            self.assertEqual(rc, 1)

    def test_watch_fail_fast_breaks_on_error_tick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = write_cases(tmp, [{"id": "pos-x", "should_trigger": True}])
            with mock.patch.dict(os.environ, {"JEV_TLINT_WATCH_MAX": "5"}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = trigger_lint.main(
                        [str(bad), "--watch", "0.01", "--fail-fast"]
                    )
            self.assertEqual(rc, 1)
            ticks = [
                json.loads(l)
                for l in buf.getvalue().splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 1)

    def test_max_ticks_flag_overrides_env_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cases(tmp, [dict(GOOD_CASE)])
            with mock.patch.dict(os.environ, {"JEV_TLINT_WATCH_MAX": "5"}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = trigger_lint.main(
                        [str(path), "--watch", "0.01", "--max-ticks", "2"]
                    )
            self.assertEqual(rc, 0)
            ticks = [
                json.loads(l)
                for l in buf.getvalue().splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 2)

    def test_max_ticks_bad_value_is_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cases(tmp, [dict(GOOD_CASE)])
            buf = io.StringIO()
            err = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(err):
                rc = trigger_lint.main(
                    [str(path), "--watch", "0.01", "--max-ticks", "nope"]
                )
            self.assertEqual(rc, 2)
            self.assertIn("max-ticks", err.getvalue())

    def test_watch_max_bad_value_is_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cases(tmp, [dict(GOOD_CASE)])
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = trigger_lint.main(
                    [str(path), "--watch", "0.01", "--watch-max", "junk"]
                )
            self.assertEqual(rc, 2)
            self.assertIn("watch-max", err.getvalue())

    def test_watch_max_seconds_bounds_loop(self) -> None:
        import time as t

        with tempfile.TemporaryDirectory() as tmp:
            path = write_cases(tmp, [dict(GOOD_CASE)])
            buf = io.StringIO()
            start = t.time()
            with redirect_stdout(buf):
                rc = trigger_lint.main(
                    [str(path), "--watch", "0.02", "--watch-max", "0.05"]
                )
            self.assertEqual(rc, 0)
            self.assertLess(t.time() - start, 2.0)
            ticks = [
                json.loads(l)
                for l in buf.getvalue().splitlines()
                if l.startswith("{")
            ]
            self.assertTrue(1 <= len(ticks) <= 5)



    def test_explain_prints_rule_description(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = trigger_lint.main(["--explain", "T011"])
        self.assertEqual(rc, 0)
        self.assertIn("T011:", buf.getvalue())
        self.assertIn("must_ask", buf.getvalue())

    def test_explain_unknown_rule_rc2(self) -> None:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = trigger_lint.main(["--explain", "T999"])
        self.assertEqual(rc, 2)


class WatchJqTests(unittest.TestCase):
    def test_watch_jq_prints_only_named_tick_field(self) -> None:
        import io as _io
        import os as _os

        buf = _io.StringIO()
        with mock.patch.dict(_os.environ, {"JEV_TLINT_WATCH_MAX": "2"}):
            with redirect_stdout(buf), redirect_stderr(_io.StringIO()):
                rc = trigger_lint.main(
                    [str(FIXTURE), "--watch", "0.01", "--jq", "findings"]
                )
        self.assertEqual(rc, 0)
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(l.isdigit() for l in lines))

class WatchSecsEnvTests(unittest.TestCase):
    def test_watch_secs_env_bounds_loop(self) -> None:
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            path = write_cases(tmp, [dict(GOOD_CASE)])
            with mock.patch.dict(
                os.environ,
                {"JEV_TLINT_WATCH_MAX": "0", "JEV_TLINT_WATCH_SECS": "0.05"},
            ):
                buf = io.StringIO()
                err = io.StringIO()
                start = _time.time()
                with redirect_stdout(buf), redirect_stderr(err):
                    rc = trigger_lint.main([str(path), "--watch", "0.02"])
            self.assertEqual(rc, 0)
            self.assertLess(_time.time() - start, 2.0)
            ticks = [
                l for l in buf.getvalue().splitlines() if l.startswith("{")
            ]
            self.assertLessEqual(len(ticks), 10)
            self.assertGreaterEqual(len(ticks), 1)

if __name__ == "__main__":
    unittest.main()

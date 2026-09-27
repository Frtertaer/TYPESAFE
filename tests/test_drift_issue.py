#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRIFT_SCRIPT = ROOT / ".github" / "scripts" / "drift_issue.py"

_spec = importlib.util.spec_from_file_location("drift_issue", DRIFT_SCRIPT)
drift_issue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drift_issue)


def eval_payload(**kw) -> dict:
    payload = {
        "total": 2,
        "rows": [
            {
                "id": "off_track",
                "before": {"noul": 0.3},
                "after": {"noul": 0.8},
                "baseline": {"noul": 0.1},
            },
            {
                "id": "market_none",
                "before": {"noul": 0.0},
                "after": {"noul": 0.5},
            },
        ],
    }
    payload.update(kw)
    return payload


def run_main(argv: list[str]):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = drift_issue.main(argv)
    return rc, out.getvalue()


class BuildIssueTests(unittest.TestCase):
    def test_clean_run_does_not_fire(self) -> None:
        body = drift_issue.build_issue(
            eval_payload(), {"verdict": "PASS", "failures": []}, [], "u"
        )
        self.assertIsNone(body)

    def test_fail_verdict_fires(self) -> None:
        body = drift_issue.build_issue(
            eval_payload(),
            {"verdict": "FAIL", "failures": ["off_track below gate"]},
            [],
            "https://ci/run/1",
        )
        self.assertIn("FAIL", body)
        self.assertIn("https://ci/run/1", body)
        self.assertIn("off_track below gate", body)
        self.assertIn("| off_track |", body)

    def test_scoring_error_fires(self) -> None:
        body = drift_issue.build_issue(
            eval_payload(error="endpoint 500"), None, []
        )
        self.assertIn("endpoint 500", body)
        self.assertIn("FAIL", body)

    def test_streak_flags_fire(self) -> None:
        payload = eval_payload(
            drift={
                "streaks": {"off_track": 3},
                "flags": ["case off_track ниже гейта 3 прогонов подряд"],
                "fails": ["case off_track ниже гейта 3 прогонов подряд"],
            }
        )
        body = drift_issue.build_issue(payload, None, [])
        self.assertIn("drift streaks", body)
        self.assertIn("3 прогонов подряд", body)
        self.assertIn("(fail-gate)", body)

    def test_acceptance_alerts_fire(self) -> None:
        body = drift_issue.build_issue(
            eval_payload(),
            {"verdict": "PASS", "failures": []},
            ["miss_rate 0.80 > miss_rate_max 0.50 (4 misses)"],
        )
        self.assertIn("miss_rate", body)
        self.assertIn("acceptance alerts", body)

    def test_diff_summary_rendered(self) -> None:
        payload = eval_payload(
            diff={
                "counts": {
                    "regressions": 1,
                    "improved": 0,
                    "changed": 0,
                    "added": 1,
                    "removed": 0,
                },
                "regressions": [
                    {"id": "off_track", "delta": -0.2, "why": "noul_regressed"}
                ],
                "added": ["new_case"],
            },
            drift={"flags": ["x"]},
        )
        body = drift_issue.build_issue(payload, None, [])
        self.assertIn("regressions: 1", body)
        self.assertIn("`off_track` (-0.20)", body)
        self.assertIn("added case: `new_case`", body)


class CliTests(unittest.TestCase):
    def test_missing_eval_file_is_silent(self) -> None:
        rc, out = run_main(["/nonexistent/eval.json"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_malformed_eval_fires(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "eval-live.json"
            bad.write_text("{not json", encoding="utf-8")
            rc, out = run_main([str(bad)])
        self.assertEqual(rc, 0)
        self.assertIn("unparseable", out)
        self.assertIn("FAIL", out)

    def test_json_mode_reports_fired(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            good = Path(td) / "eval-live.json"
            good.write_text(json.dumps(eval_payload()), encoding="utf-8")
            rc, out = run_main([str(good), "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["fired"], False)

    def test_out_file_written_only_when_fired(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ev = Path(td) / "eval-live.json"
            target = Path(td) / "issue.md"
            ev.write_text(json.dumps(eval_payload()), encoding="utf-8")
            rc, _ = run_main([str(ev), "--out", str(target)])
            self.assertEqual(rc, 0)
            self.assertFalse(target.exists())
            ev.write_text(
                json.dumps(eval_payload(error="boom")), encoding="utf-8"
            )
            rc, _ = run_main([str(ev), "--out", str(target)])
            self.assertEqual(rc, 0)
            self.assertIn("boom", target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

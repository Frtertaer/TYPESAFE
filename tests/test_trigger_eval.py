#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "jev-consult" / "scripts" / "trigger_eval.py"
FIXTURE = ROOT / "tests" / "fixtures" / "jev-consult.trigger-cases.json"
SKILL = ROOT / "skills" / "jev-consult"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


te = load(SCRIPT, "jev_trigger_eval")


def write_cases(tmp: str, cases: list[dict]) -> Path:
    path = Path(tmp) / "cases.json"
    path.write_text(json.dumps({"cases": cases}), encoding="utf-8")
    return path


class TriggerEvalTests(unittest.TestCase):
    def test_fixture_eval_passes(self) -> None:
        result = te.evaluate(FIXTURE, SKILL)
        self.assertIsNotNone(result)
        self.assertTrue(result["ok"])
        self.assertGreater(result["n_positives"], 0)
        self.assertGreater(result["n_negatives"], 0)

    def test_rows_cover_every_case(self) -> None:
        result = te.evaluate(FIXTURE, SKILL)
        ids = [c["id"] for c in json.loads(FIXTURE.read_text())["cases"]]
        self.assertEqual([r["id"] for r in result["cases"]], ids)
        for row in result["cases"]:
            if row["lexical"]:
                self.assertIsNotNone(row["score"])
            else:
                self.assertIsNone(row["score"])

    def test_weak_positive_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cases = write_cases(
                tmp,
                [
                    {
                        "id": "pos-weak",
                        "prompt": "zebra giraffe pineapple",
                        "should_trigger": True,
                    },
                    {
                        "id": "neg-strong",
                        "prompt": "jev consult routing decision",
                        "should_trigger": False,
                    },
                ],
            )
            result = te.evaluate(cases, SKILL)
            self.assertIsNotNone(result)
            self.assertFalse(result["ok"])

    def test_cli_json_and_quiet(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"])
        self.assertIn("cases", payload)

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--quiet"])
        self.assertEqual(rc, 0)
        self.assertIn("margin:", buf.getvalue())
        self.assertNotIn("should_trigger=", buf.getvalue())

    def test_out_writes_result_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "eval.json"
            buf = io.StringIO()
            with redirect_stdout(buf), patch("sys.stderr", io.StringIO()):
                rc = te.main(["--quiet", "--out", str(target)])
            self.assertEqual(rc, 0)
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual(len(payload["cases"]), len(payload["cases"]))

    def test_fail_flag_filters_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cases = write_cases(
                tmp,
                [
                    {
                        "id": "pos-dead",
                        "prompt": "zzz qqq xxx",
                        "should_trigger": True,
                    },
                    {
                        "id": "neg-x",
                        "prompt": "unrelated words here",
                        "should_trigger": False,
                    },
                ],
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = te.main(["--cases", str(cases), "--fail"])
            self.assertEqual(rc, 1)
            self.assertIn("pos-dead", buf.getvalue())
            self.assertNotIn("neg-x", buf.getvalue())
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = te.main(["--cases", str(cases), "--fail", "--json"])
            payload = json.loads(buf.getvalue())
            self.assertEqual([r["id"] for r in payload["cases"]], ["pos-dead"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = te.main(["--fail"])
            self.assertEqual(rc, 0)
            self.assertNotIn("should_trigger=", buf.getvalue())

    def test_ids_prints_case_ids(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--ids"])
        self.assertEqual(rc, 0)
        lines = buf.getvalue().strip().splitlines()
        self.assertIn("neg-loop-bug", lines)
        self.assertTrue(all(line.startswith(("pos-", "neg-")) for line in lines))
        with tempfile.TemporaryDirectory() as tmp:
            cases = write_cases(
                tmp,
                [
                    {
                        "id": "pos-dead",
                        "prompt": "zzz qqq xxx",
                        "should_trigger": True,
                    },
                    {
                        "id": "neg-x",
                        "prompt": "unrelated words here",
                        "should_trigger": False,
                    },
                ],
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = te.main(["--cases", str(cases), "--ids", "--fail"])
            self.assertEqual(rc, 1)
            self.assertEqual(buf.getvalue().strip(), "pos-dead")

    def test_id_evaluates_single_case(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--id", "pos-approach"])
        self.assertEqual(rc, 0)
        self.assertIn("pos-approach", buf.getvalue())
        self.assertEqual(buf.getvalue().count("should_trigger="), 1)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--id", "neg-git"])
        self.assertEqual(rc, 0)
        self.assertIn("neg-git", buf.getvalue())
        with patch("sys.stderr", io.StringIO()):
            rc = te.main(["--id", "nope"])
        self.assertEqual(rc, 2)

    def test_score_adhoc_prompt(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--score", "which approach should I pick"])
        self.assertEqual(rc, 0)
        self.assertIn("score=", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--score", "zzz qqq", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["prompt"], "zzz qqq")
        self.assertIsInstance(payload["score"], float)

    def test_min_score_filters_rows(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--min-score", "1.0", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["cases"])
        self.assertTrue(
            all(r["score"] is not None and r["score"] >= 1.0 for r in payload["cases"])
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            te.main(["--min-score", "99"])
        self.assertIn("margin:", buf.getvalue())
        self.assertNotIn("should_trigger=", buf.getvalue())

    def test_desc_overrides_skill_description(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--score", "jev consult", "--desc", "banana bread recipes"])
        self.assertEqual(rc, 0)
        score_off = buf.getvalue()
        buf = io.StringIO()
        with redirect_stdout(buf):
            te.main(["--score", "jev consult"])
        score_on = buf.getvalue()
        self.assertNotEqual(score_off, score_on)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--desc", "unrelated plumbing text", "--json"])
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload["ok"])

    def test_strict_fails_on_scored_negative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cases(
                tmp,
                [
                    {
                        "id": "p",
                        "prompt": "jev consult pick decide between options choose approach wisely",
                        "should_trigger": True,
                    },
                    {
                        "id": "n",
                        "prompt": "decision approach",
                        "should_trigger": False,
                    },
                ],
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = te.main(["--cases", str(path)])
            self.assertEqual(rc, 0)  # margin passes (1.333 > 0.707*1.15)
            # strict still fails because the negative scored
            with redirect_stdout(buf):
                rc = te.main(["--cases", str(path), "--strict"])
            self.assertEqual(rc, 1)

    def test_csv_prints_table(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--csv"])
        self.assertEqual(rc, 0)
        lines = buf.getvalue().splitlines()
        self.assertEqual(lines[0], "id,should_trigger,lexical,score,ok")
        self.assertGreater(len(lines), 2)
        row = [l for l in lines if l.startswith("pos-approach,")][0]
        self.assertEqual(row.split(",")[1], "True")

    def test_covers_reports_tag_counts_and_uncovered(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--covers"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("approach 2\n", out)
        self.assertIn("uncovered: neg-format\n", out)

    def test_covers_map_lists_ids_per_tag(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--covers-map"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("approach: pos-approach, pos-remember\n", out)
        self.assertIn("keep_vs_change: ", out)
        self.assertIn("uncovered: neg-format\n", out)

    def test_tokens_shows_matched_tokens(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--id", "pos-approach", "--tokens"])
        self.assertEqual(rc, 0)
        self.assertIn("tokens=", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            te.main(["--id", "pos-approach", "--tokens", "--json"])
        row = json.loads(buf.getvalue())["cases"][0]
        self.assertIn("matched", row)
        self.assertTrue(row["matched"])

    def test_unmatched_shows_missed_tokens(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--id", "pos-approach", "--unmatched"])
        self.assertEqual(rc, 0)
        self.assertIn("missed=", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            te.main(["--id", "pos-approach", "--unmatched", "--json"])
        row = json.loads(buf.getvalue())["cases"][0]
        self.assertIn("unmatched", row)
        self.assertNotIn("zzz", row["matched"])
        self.assertEqual(
            set(row["matched"]) | set(row["unmatched"]),
            set(row["matched"] + row["unmatched"]),
        )

    def test_prompts_lists_case_prompts(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--prompts"])
        self.assertEqual(rc, 0)
        lines = buf.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("pos-approach: "))
        self.assertTrue(any(l.startswith("neg-format: ") for l in lines))

    def test_summary_prints_aggregate_only(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--summary"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("margin: PASS", out)
        self.assertIn("positives=", out)
        self.assertIn("negatives=", out)
        self.assertNotIn("should_trigger=", out)

    def test_desc_tokens_prints_token_set(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--desc-tokens"])
        self.assertEqual(rc, 0)
        out = buf.getvalue().splitlines()
        self.assertTrue(out)
        self.assertEqual(out, sorted(set(out)))
        buf = io.StringIO()
        with redirect_stdout(buf):
            te.main(["--desc-tokens", "--desc", "pick between two options"])
        self.assertEqual(buf.getvalue().splitlines(), ["between", "option", "pick", "two"])

    def test_margin_override_flips_verdict(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--summary"])
        self.assertEqual(rc, 0)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--summary", "--margin", "5"])
        self.assertEqual(rc, 1)
        self.assertIn("x 5.00", buf.getvalue())

    def test_dist_prints_histogram(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--dist"])
        self.assertEqual(rc, 0)
        lines = buf.getvalue().splitlines()
        self.assertTrue(lines)
        for line in lines:
            if line.startswith("unscored"):
                continue
            lo_hi, _, count = line.partition(" ")
            lo, hi = lo_hi.split("-")
            self.assertAlmostEqual(float(hi) - float(lo), 0.25)
            self.assertTrue(int(count) > 0)
        self.assertIn("unscored 1\n", buf.getvalue())

    def test_sort_orders_rows_by_score(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--sort", "--json"])
        self.assertEqual(rc, 0)
        scores = [r["score"] for r in json.loads(buf.getvalue())["cases"]]
        scored = [s for s in scores if s is not None]
        self.assertEqual(scored, sorted(scored))
        self.assertTrue(all(s is None for s in scores[len(scored):]))

    def test_top_limits_to_weakest_n(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--top", "3", "--json"])
        self.assertEqual(rc, 0)
        cases = json.loads(buf.getvalue())["cases"]
        self.assertEqual(len(cases), 3)
        scores = [r["score"] for r in cases]
        scored = [s for s in scores if s is not None]
        self.assertEqual(scored, sorted(scored))
        self.assertTrue(all(s is None for s in scores[len(scored):]))

        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            te.main(["--top", "2", "--sort", "--json"])
        all_scores = [
            r["score"] for r in json.loads(buf2.getvalue())["cases"] if r["score"] is not None
        ]
        full = io.StringIO()
        with redirect_stdout(full):
            te.main(["--sort", "--json"])
        full_scores = [
            s
            for s in (r["score"] for r in json.loads(full.getvalue())["cases"])
            if s is not None
        ]
        self.assertEqual(all_scores, full_scores[:2])

    def test_min_covers_fails_thin_tags(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--min-covers", "1", "--quiet"])
        self.assertEqual(rc, 0)
        with redirect_stdout(buf):
            rc = te.main(["--min-covers", "4", "--quiet"])
        self.assertEqual(rc, 1)
        with redirect_stdout(buf):
            te.main(["--min-covers", "4", "--covers"])
        self.assertIn("below --min-covers", buf.getvalue())

    def test_report_writes_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.md"
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = te.main(["--report", str(path), "--quiet"])
            self.assertEqual(rc, 0)
            text = path.read_text(encoding="utf-8")
            self.assertIn("verdict: **PASS**", text)
            self.assertIn("- positives: 16", text)
            self.assertIn("| pos-approach | True | True |", text)

    def test_positive_cases_declare_covers(self) -> None:
        cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
        missing = [
            c["id"] for c in cases if c.get("should_trigger") and not c.get("covers")
        ]
        self.assertEqual(missing, [])

    def test_env_reports_resolved_config(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = te.main(["--env"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["cases_exists"])
        self.assertTrue(payload["scorer_exists"])
        self.assertEqual(payload["margin"], payload["margin_default"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            te.main(["--env", "--margin", "2.5", "--desc", "x"])
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["margin"], 2.5)
        self.assertTrue(payload["desc_override"])

    def test_watch_emits_ticks(self) -> None:
        buf = io.StringIO()
        with patch.dict(os.environ, {"JEV_TRIGGER_WATCH_MAX": "2"}):
            with redirect_stdout(buf):
                rc = te.main(["--watch", "0.01", "--quiet"])
        self.assertEqual(rc, 0)
        ticks = [
            json.loads(l) for l in buf.getvalue().splitlines() if l.startswith("{")
        ]
        self.assertEqual(len(ticks), 2)
        self.assertTrue(all(t["ok"] for t in ticks))
        self.assertIn("worst_positive", ticks[0])

    def test_missing_cases_file_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("sys.stderr", io.StringIO()):
                rc = te.main(["--cases", str(Path(tmp) / "nope.json")])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()

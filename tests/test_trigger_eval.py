#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import io
import json
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

    def test_missing_cases_file_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("sys.stderr", io.StringIO()):
                rc = te.main(["--cases", str(Path(tmp) / "nope.json")])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()

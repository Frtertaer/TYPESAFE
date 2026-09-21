#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import compact  # noqa: E402
import compare  # noqa: E402
import trace  # noqa: E402

EXAMPLES = ROOT / "skills" / "jev-consult" / "examples"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ExampleFilesTests(unittest.TestCase):
    def test_every_json_parses(self) -> None:
        found = list(EXAMPLES.glob("*.json"))
        self.assertGreaterEqual(len(found), 5)
        for path in found:
            json.loads(path.read_text(encoding="utf-8"))

    def test_compact_transcript_parses_to_messages(self) -> None:
        raw = (EXAMPLES / "compact-transcript.json").read_text(encoding="utf-8")
        messages = compact.parse_transcript(raw)
        self.assertTrue(messages)

    def test_compare_cases_load_and_score(self) -> None:
        blob = compare.load_cases(EXAMPLES / "compare-cases.json")
        cases = blob["cases"]
        self.assertTrue(cases)
        for case in cases:
            row = compare.row_offline(case)
            self.assertIn("id", row)

    def test_trace_template_loads(self) -> None:
        data = json.loads((EXAMPLES / "trace.template.json").read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)
        self.assertIn("plan", data)
        self.assertIn("history", data)


if __name__ == "__main__":
    unittest.main()

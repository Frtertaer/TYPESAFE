#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_trace():
    path = ROOT / "skills" / "jev-consult" / "scripts" / "trace.py"
    spec = importlib.util.spec_from_file_location("jev_trace", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


tr = load_trace()


class TraceTests(unittest.TestCase):
    def test_merge_keeps_trace_plan_when_state_forgets(self) -> None:
        trace = tr.empty()
        trace["plan"] = "Add JWT auth."
        trace["last_pick"] = "pyjwt"
        merged = tr.merge_state({"unknown": "next file"}, trace)
        self.assertEqual(merged["plan"], "Add JWT auth.")
        self.assertEqual(merged["unknown"], "next file")
        self.assertEqual(merged["last_pick"], "pyjwt")

    def test_merge_non_dict_state(self) -> None:
        merged = tr.merge_state("just a note", {"plan": "A"})
        self.assertEqual(merged["request"], "just a note")
        self.assertEqual(merged["trace"]["plan"], "A")

    def test_bump_and_record_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            old = os.environ.get("JEV_TRACE")
            os.environ["JEV_TRACE"] = str(path)
            try:
                self.assertEqual(tr.main(["init", "--plan", "Add tests."]), 0)
                self.assertEqual(tr.main(["bump", "--error", "TypeError"]), 0)
                self.assertEqual(
                    tr.main(["record", "--pick", "return_to_plan", "--kind", "next_move"]),
                    0,
                )
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(data["plan"], "Add tests.")
                self.assertEqual(data["attempt_count"], 1)
                self.assertEqual(data["last_error"], "TypeError")
                self.assertEqual(data["last_pick"], "return_to_plan")
                self.assertEqual(data["history"][0]["kind"], "next_move")
            finally:
                if old is None:
                    os.environ.pop("JEV_TRACE", None)
                else:
                    os.environ["JEV_TRACE"] = old

    def test_load_missing_is_empty(self) -> None:
        data = tr.load(Path("definitely-missing-jev-trace.json"))
        self.assertEqual(data["plan"], "")
        self.assertEqual(data["attempt_count"], 0)

    def test_jev_apply_trace_fills_forgotten_plan(self) -> None:
        jev_path = ROOT / "skills" / "jev-consult" / "scripts" / "jev.py"
        spec = importlib.util.spec_from_file_location("jev_consult_jev", jev_path)
        jev = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(jev)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(
                json.dumps({"plan": "Add JWT auth.", "last_pick": "pyjwt"}),
                encoding="utf-8",
            )
            merged = jev.apply_trace({"unknown": "next file"}, str(path))
            self.assertEqual(merged["plan"], "Add JWT auth.")
            self.assertEqual(merged["unknown"], "next file")


if __name__ == "__main__":
    sys.exit(0 if unittest.main(verbosity=2) else 1)

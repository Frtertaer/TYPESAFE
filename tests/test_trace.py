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

    def test_load_corrupt_json_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text("{truncated", encoding="utf-8")
            data = tr.load(path)
            self.assertEqual(data, tr.empty())

    def test_load_non_dict_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            self.assertEqual(tr.load(path), tr.empty())

    def test_load_keeps_only_known_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(
                json.dumps(
                    {
                        "plan": "A",
                        "surprise": "dropped",
                        "attempt_count": "3",
                        "inspected": "not a list",
                        "history": {"nope": True},
                    }
                ),
                encoding="utf-8",
            )
            data = tr.load(path)
            self.assertEqual(data["plan"], "A")
            self.assertNotIn("surprise", data)
            self.assertEqual(data["attempt_count"], 3)
            self.assertEqual(data["inspected"], [])
            self.assertEqual(data["history"], [])

    def test_load_bad_attempt_count_coerced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(json.dumps({"attempt_count": "lots"}), encoding="utf-8")
            self.assertEqual(tr.load(path)["attempt_count"], 0)

    def test_merge_state_empty_values_keep_trace(self) -> None:
        trace = tr.empty()
        trace["plan"] = "the plan"
        trace["current_step"] = "step 2"
        merged = tr.merge_state({"plan": "", "current_step": None, "inspected": [], "extra": {}}, trace)
        self.assertEqual(merged["plan"], "the plan")
        self.assertEqual(merged["current_step"], "step 2")
        self.assertEqual(merged["inspected"], [])
        # present values still win
        merged = tr.merge_state({"plan": "override"}, trace)
        self.assertEqual(merged["plan"], "override")

    def test_record_caps_history_at_20(self) -> None:
        data = tr.empty()
        for i in range(25):
            data = tr.record(data, pick="p%d" % i)
        self.assertEqual(len(data["history"]), 20)
        self.assertEqual(data["history"][0]["pick"], "p5")
        self.assertEqual(data["last_pick"], "p24")
        self.assertNotIn("kind", data["history"][-1])

    def test_bump_without_error_keeps_last_error(self) -> None:
        data = tr.bump({"attempt_count": 2, "last_error": "old"}, error="")
        self.assertEqual(data["attempt_count"], 3)
        self.assertEqual(data["last_error"], "old")

    def test_cli_show_and_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            rc = tr.main(["--file", str(path), "show"])
            self.assertEqual(rc, 0)
            self.assertFalse(path.exists())
            rc = tr.main(
                [
                    "--file",
                    str(path),
                    "set",
                    "--plan",
                    "P",
                    "--step",
                    "S",
                    "--unknown",
                    "U",
                    "--error",
                    "E",
                    "--attempt",
                    "4",
                ]
            )
            self.assertEqual(rc, 0)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                (data["plan"], data["current_step"], data["unknown"], data["last_error"], data["attempt_count"]),
                ("P", "S", "U", "E", 4),
            )
            # set with no flags leaves fields untouched
            rc = tr.main(["--file", str(path), "set"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["plan"], "P")

    def test_cli_record_with_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            rc = tr.main(["--file", str(path), "record", "--pick", "ask_human", "--step", "blocked"])
            self.assertEqual(rc, 0)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["last_pick"], "ask_human")
            self.assertEqual(data["current_step"], "blocked")

    def test_state_subcommand_stdout_and_out(self) -> None:
        import io
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(
                json.dumps(
                    {
                        "plan": "Add tests.",
                        "current_step": "writing",
                        "attempt_count": 2,
                        "inspected": [],
                        "history": [{"pick": "a"}],
                        "last_error": "",
                    }
                ),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = tr.main(["--file", str(path), "state"])
            self.assertEqual(rc, 0)
            state = json.loads(buf.getvalue())
            # bare dict, empty values stripped
            self.assertEqual(state["plan"], "Add tests.")
            self.assertNotIn("inspected", state)
            self.assertNotIn("last_error", state)
            self.assertIn("history", state)
            # --out writes scaffold-ready file
            out = Path(tmp) / "state.json"
            with patch.object(sys, "stdout", io.StringIO()):
                tr.main(["--file", str(path), "state", "--out", str(out)])
            written = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(written, state)

    def test_state_missing_file_empty(self) -> None:
        import io
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = tr.main(["--file", str(Path(tmp) / "none.json"), "state"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue()), {"attempt_count": 0})

    def test_state_feeds_scaffold(self) -> None:
        # state --out output loads as jev.py scaffold --state input
        jev_path = ROOT / "skills" / "jev-consult" / "scripts" / "jev.py"
        spec = importlib.util.spec_from_file_location("jev_state_feed", jev_path)
        jev = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(jev)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(json.dumps({"plan": "P", "attempt_count": 3}), encoding="utf-8")
            out = Path(tmp) / "state.json"
            import io
            from unittest.mock import patch

            with patch.object(sys, "stdout", io.StringIO()):
                tr.main(["--file", str(path), "state", "--out", str(out)])
            loaded = jev.load_scaffold_state(str(out), "fallback")
            self.assertEqual(loaded["plan"], "P")
            self.assertEqual(loaded["attempt_count"], 3)

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

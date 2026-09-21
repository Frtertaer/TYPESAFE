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

    def test_stats_missing_file(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            buf = StringIO()
            with redirect_stdout(buf):
                rc = tr.main(["--file", str(Path(tmp) / "none.json"), "stats"])
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertFalse(out["exists"])
            self.assertEqual(out["attempt_count"], 0)
            self.assertEqual(out["history"], 0)
            self.assertNotIn("age_seconds", out)

    def test_stats_filled_trace(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.save(
                {
                    "plan": "p",
                    "attempt_count": 3,
                    "last_pick": "jwt-auth",
                    "last_error": "boom",
                    "unknown": "x",
                    "inspected": ["a", "b"],
                    "history": [{"pick": "jwt-auth"}],
                },
                path,
            )
            buf = StringIO()
            with redirect_stdout(buf):
                rc = tr.main(["--file", str(path), "stats"])
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertTrue(out["exists"])
            self.assertEqual(out["attempt_count"], 3)
            self.assertEqual(out["last_pick"], "jwt-auth")
            self.assertEqual(out["inspected"], 2)
            self.assertEqual(out["history"], 1)
            self.assertTrue(out["has_error"])
            self.assertTrue(out["has_unknown"])
            self.assertGreaterEqual(out["age_seconds"], 0)

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

    def test_cli_set_kv_merges_arbitrary_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            rc = tr.main(["--file", str(path), "init", "--plan", "P"])
            self.assertEqual(rc, 0)
            rc = tr.main(
                [
                    "--file",
                    str(path),
                    "set",
                    "--kv",
                    "custom= val 1 ",
                    "--kv",
                    "branch=devin/x",
                    "--kv",
                    "no-equals-ignored",
                ]
            )
            self.assertEqual(rc, 0)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["custom"], "val 1")
            self.assertEqual(data["branch"], "devin/x")
            self.assertNotIn("no-equals-ignored", data)
            self.assertEqual(data["plan"], "P")

    def test_cli_record_with_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            rc = tr.main(["--file", str(path), "record", "--pick", "ask_human", "--step", "blocked"])
            self.assertEqual(rc, 0)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["last_pick"], "ask_human")
            self.assertEqual(data["current_step"], "blocked")

    def test_cli_record_note_appends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.main(["--file", str(path), "init", "--plan", "P"])
            rc = tr.main(
                [
                    "--file",
                    str(path),
                    "record",
                    "--pick",
                    "ask_human",
                    "--note",
                    "waiting on CI",
                ]
            )
            self.assertEqual(rc, 0)
            tr.main(
                [
                    "--file",
                    str(path),
                    "record",
                    "--pick",
                    "retry",
                    "--note",
                    "second",
                ]
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(data["notes"]), 2)
            self.assertEqual(data["notes"][0]["text"], "waiting on CI")
            self.assertRegex(data["notes"][0]["iso"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            self.assertGreater(data["notes"][0]["ts"], 0)
            self.assertEqual(data["notes"][1]["text"], "second")

    def test_cli_notes_lists_notes(self) -> None:
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.main(["--file", str(path), "init", "--plan", "P"])
            tr.main(["--file", str(path), "record", "--pick", "x", "--note", "hello"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tr.main(["--file", str(path), "notes"])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("hello", out)
            self.assertIn("1 note(s)", out)

    def test_cli_notes_json(self) -> None:
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.main(["--file", str(path), "init", "--plan", "P"])
            tr.main(["--file", str(path), "record", "--pick", "x", "--note", "n1"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tr.main(["--file", str(path), "notes", "--json"])
            self.assertEqual(rc, 0)
            rows = json.loads(buf.getvalue())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["text"], "n1")
            self.assertIn("iso", rows[0])

    def test_cli_notes_field_prints_field_only(self) -> None:
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.main(["--file", str(path), "init", "--plan", "P"])
            tr.main(["--file", str(path), "record", "--pick", "x", "--note", "hello"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tr.main(["--file", str(path), "notes", "--field", "text"])
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue().strip(), "hello")
            buf = io.StringIO()
            with redirect_stdout(buf):
                tr.main(["--file", str(path), "notes", "--field", "nope"])
            self.assertEqual(buf.getvalue().strip(), "null")
            buf = io.StringIO()
            with redirect_stdout(buf):
                tr.main(["--file", str(path), "notes", "--field", "text", "--json"])
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["values"], ["hello"])

    def test_cli_notes_limit_caps_output(self) -> None:
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.main(["--file", str(path), "init", "--plan", "P"])
            for text in ("a", "b", "c"):
                tr.main(["--file", str(path), "record", "--pick", "x", "--note", text])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tr.main(["--file", str(path), "notes", "--limit", "2"])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertNotIn("a", out.split(" note(s)")[0].split("\n")[0])
            self.assertIn("b", out)
            self.assertIn("c", out)
            self.assertIn("2 note(s)", out)

    def test_cli_history_lists_picks(self) -> None:
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.main(["--file", str(path), "init", "--plan", "P"])
            tr.main(["--file", str(path), "record", "--pick", "jwt-auth", "--kind", "skill"])
            tr.main(["--file", str(path), "record", "--pick", "sqlite", "--kind", "mcp"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tr.main(["--file", str(path), "history"])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("skill jwt-auth", out)
            self.assertIn("mcp sqlite", out)
            self.assertIn("2 pick(s)", out)
            buf = io.StringIO()
            with redirect_stdout(buf):
                tr.main(["--file", str(path), "history", "--limit", "1"])
            self.assertIn("sqlite", buf.getvalue())
            self.assertNotIn("jwt-auth", buf.getvalue())
            self.assertIn("1 pick(s)", buf.getvalue())
            buf = io.StringIO()
            with redirect_stdout(buf):
                tr.main(["--file", str(path), "history", "--json"])
            rows = json.loads(buf.getvalue())
            self.assertEqual([r["pick"] for r in rows], ["jwt-auth", "sqlite"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tr.main(["--file", str(path), "history", "--field", "kind"])
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue().split(), ["skill", "mcp"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tr.main(["--file", str(path), "history", "--field", "missing", "--json"])
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["field"], "missing")
            self.assertEqual(payload["values"], [None, None])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tr.main(["--file", str(path), "history", "--since", "9999999999"])
            self.assertEqual(rc, 0)
            self.assertIn("0 pick(s)", buf.getvalue())
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tr.main(["--file", str(path), "history", "--before", "9999999999"])
            self.assertEqual(rc, 0)
            self.assertIn("2 pick(s)", buf.getvalue())
            rc = tr.main(["--file", str(path), "history", "--since", "bogus"])
            self.assertEqual(rc, 2)

    def test_cli_notes_out_writes_file(self) -> None:
        import io
        from contextlib import redirect_stderr, redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.main(["--file", str(path), "init", "--plan", "P"])
            tr.main(["--file", str(path), "record", "--pick", "x", "--note", "hello"])
            out_path = Path(tmp) / "notes.txt"
            buf, err = io.StringIO(), io.StringIO()
            with redirect_stdout(buf), redirect_stderr(err):
                rc = tr.main(["--file", str(path), "notes", "--out", str(out_path)])
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue(), "")
            self.assertIn("wrote 1 note(s)", err.getvalue())
            written = out_path.read_text(encoding="utf-8")
            self.assertIn("hello", written)
            self.assertIn("1 note(s)", written)

    def test_cli_notes_since_filters_old(self) -> None:
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(
                json.dumps(
                    {
                        "notes": [
                            {"ts": 100.0, "iso": "x", "text": "old-note"},
                            {"ts": 200.0, "iso": "y", "text": "new-note"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tr.main(["--file", str(path), "notes", "--since", "150"])
            self.assertEqual(rc, 0)
            self.assertIn("new-note", buf.getvalue())
            self.assertNotIn("old-note", buf.getvalue())
            rc = tr.main(["--file", str(path), "notes", "--since", "bogus"])
            self.assertEqual(rc, 2)

    def test_cli_notes_before_filters_new(self) -> None:
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(
                json.dumps(
                    {
                        "notes": [
                            {"ts": 100.0, "iso": "x", "text": "old-note"},
                            {"ts": 200.0, "iso": "y", "text": "new-note"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tr.main(["--file", str(path), "notes", "--before", "150"])
            self.assertEqual(rc, 0)
            self.assertIn("old-note", buf.getvalue())
            self.assertNotIn("new-note", buf.getvalue())
            rc = tr.main(["--file", str(path), "notes", "--before", "bogus"])
            self.assertEqual(rc, 2)

    def test_cli_notes_harness_tags_and_filters(self) -> None:
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.main(["--file", str(path), "init", "--plan", "P"])
            tr.main(
                ["--file", str(path), "record", "--pick", "x", "--note", "for-hermes", "--harness", "hermes"]
            )
            tr.main(["--file", str(path), "record", "--pick", "x", "--note", "plain-note"])
            data = json.loads(path.read_text(encoding="utf-8"))
            tagged = [n for n in data["notes"] if n.get("harness") == "hermes"]
            self.assertEqual(len(tagged), 1)
            self.assertEqual(tagged[0]["text"], "for-hermes")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tr.main(["--file", str(path), "notes", "--harness", "hermes"])
            self.assertEqual(rc, 0)
            self.assertIn("for-hermes", buf.getvalue())
            self.assertNotIn("plain-note", buf.getvalue())

    def test_cli_init_plan_env_default(self) -> None:
        import os as _os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            with patch.dict(_os.environ, {"JEV_TRACE_PLAN": "env-plan"}):
                rc = tr.main(["--file", str(path), "init"])
            self.assertEqual(rc, 0)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["plan"], "env-plan")
            rc = tr.main(["--file", str(Path(tmp) / "none.json"), "init"])
            self.assertEqual(rc, 2)

    def test_cli_notes_prune_rewrites_file(self) -> None:
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.main(["--file", str(path), "init", "--plan", "P"])
            for text in ("a", "b", "c"):
                tr.main(["--file", str(path), "record", "--pick", "x", "--note", text])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tr.main(["--file", str(path), "notes", "--prune", "1"])
            self.assertEqual(rc, 0)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([n["text"] for n in data["notes"]], ["c"])

    def test_cli_record_note_stdin_dash(self) -> None:
        import io
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            with patch("sys.stdin", io.StringIO("from stdin\n")):
                tr.main(["--file", str(path), "record", "--pick", "x", "--note", "-"])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["notes"][0]["text"], "from stdin")

    def test_cli_record_note_env_default(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            with patch.dict(os.environ, {"JEV_TRACE_NOTE": "env-note"}):
                tr.main(["--file", str(path), "record", "--pick", "x"])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["notes"][0]["text"], "env-note")
            with patch.dict(os.environ, {"JEV_TRACE_NOTE": "env-note"}):
                tr.main(
                    ["--file", str(path), "record", "--pick", "y", "--note", "cli-note"]
                )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["notes"][-1]["text"], "cli-note")

    def test_cli_record_no_note_no_notes_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.main(["--file", str(path), "record", "--pick", "x"])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["notes"], [])

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

    def test_show_reports_age(self) -> None:
        import io
        import os
        import time
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = tr.main(["--file", str(path), "show"])
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertFalse(out["exists"])
            self.assertIsNone(out["age_seconds"])

            with patch.object(sys, "stdout", io.StringIO()):
                tr.main(["--file", str(path), "init", "--plan", "P"])
            old = time.time() - 600
            os.utime(path, (old, old))
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                tr.main(["--file", str(path), "show"])
            out = json.loads(buf.getvalue())
            self.assertTrue(out["exists"])
            self.assertGreaterEqual(out["age_seconds"], 590)

    def test_show_pretty_text(self) -> None:
        import io
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            with patch.object(sys, "stdout", io.StringIO()):
                tr.main(["--file", str(path), "init", "--plan", "Add tests.", "--step", "writing"])
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = tr.main(["--file", str(path), "show", "--pretty"])
            self.assertEqual(rc, 0)
            text = buf.getvalue()
            self.assertIn("file: ", text)
            self.assertIn("age: ", text)
            self.assertIn("plan: Add tests.", text)
            self.assertIn("current_step: writing", text)

    def test_show_pretty_missing(self) -> None:
        import io
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "none.json"
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = tr.main(["--file", str(path), "show", "--pretty"])
            self.assertEqual(rc, 0)
            self.assertIn("(missing)", buf.getvalue())
            self.assertNotIn("age:", buf.getvalue())

    def test_show_key_scalar(self) -> None:
        import io
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            with patch.object(sys, "stdout", io.StringIO()):
                tr.main(["--file", str(path), "init", "--plan", "Add tests.", "--step", "writing"])
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = tr.main(["--file", str(path), "show", "--key", "plan"])
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue().strip(), "Add tests.")

    def test_show_key_missing_blank(self) -> None:
        import io
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            with patch.object(sys, "stdout", io.StringIO()):
                tr.main(["--file", str(path), "init", "--plan", "P"])
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = tr.main(["--file", str(path), "show", "--key", "nope"])
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue(), "\n")

    def test_show_key_dict_value_json(self) -> None:
        import io
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(
                json.dumps({"plan": "p", "last_pick": {"name": "x", "kind": "skill"}}),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = tr.main(["--file", str(path), "show", "--key", "last_pick"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue())["name"], "x")

    def test_prune_removes_only_stale(self) -> None:
        import io
        import os
        import time
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            with patch.object(sys, "stdout", io.StringIO()):
                tr.main(["--file", str(path), "init", "--plan", "P"])
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = tr.main(["--file", str(path), "prune", "--older-than", "3600"])
            self.assertEqual(rc, 0)
            self.assertFalse(json.loads(buf.getvalue())["removed"])
            self.assertTrue(path.is_file())
            old = time.time() - 7200
            os.utime(path, (old, old))
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = tr.main(["--file", str(path), "prune", "--older-than", "3600"])
            self.assertEqual(rc, 0)
            self.assertTrue(json.loads(buf.getvalue())["removed"])
            self.assertFalse(path.exists())

    def test_prune_missing_file_ok(self) -> None:
        import io
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = tr.main(
                    ["--file", str(Path(tmp) / "none.json"), "prune", "--older-than", "1"]
                )
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue())["reason"], "missing")

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

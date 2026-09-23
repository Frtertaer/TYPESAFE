#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
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

    def test_init_requires_plan_and_bump_monotonic(self) -> None:
        """init needs --plan (or JEV_TRACE_PLAN); bump increments and a
        re-init resets the attempt counter (fresh trace semantics)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            old = os.environ.get("JEV_TRACE")
            os.environ["JEV_TRACE"] = str(path)
            os.environ.pop("JEV_TRACE_PLAN", None)
            try:
                self.assertEqual(tr.main(["init"]), 2)
                self.assertFalse(path.exists())
                self.assertEqual(tr.main(["init", "--plan", "P"]), 0)
                self.assertEqual(tr.main(["bump", "--error", "E1"]), 0)
                self.assertEqual(tr.main(["bump"]), 0)
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(data["attempt_count"], 2)
                self.assertEqual(data["last_error"], "E1")
                self.assertEqual(tr.main(["init", "--plan", "P2"]), 0)
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(data["attempt_count"], 0)
                self.assertEqual(data["plan"], "P2")
            finally:
                if old is None:
                    os.environ.pop("JEV_TRACE", None)
                else:
                    os.environ["JEV_TRACE"] = old

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

    def test_self_test_roundtrips_a_pick(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        buf = StringIO()
        with redirect_stdout(buf):
            rc = tr.main(["self-test"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["self_test"], "ok")
        self.assertEqual(payload["last_pick"], "self-test-pick")
        self.assertEqual(payload["history"], 1)

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

    def test_stats_reports_entry_bytes(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.save(
                {
                    "history": [{"pick": "a", "ts": 1, "kind": "idf"}],
                    "notes": [{"ts": 1, "text": "héllo"}],
                },
                path,
            )
            buf = StringIO()
            with redirect_stdout(buf):
                rc = tr.main(["--file", str(path), "stats"])
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertEqual(out["notes"], 1)
            self.assertEqual(
                out["notes_bytes"],
                len(json.dumps({"ts": 1, "text": "héllo"}, ensure_ascii=False).encode("utf-8")),
            )
            self.assertEqual(
                out["history_bytes"],
                len(json.dumps({"pick": "a", "ts": 1, "kind": "idf"}, ensure_ascii=False).encode("utf-8")),
            )

    def test_stats_jq_prints_one_field(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout, redirect_stderr

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.save({"plan": "p", "attempt_count": 3}, path)
            buf = StringIO()
            with redirect_stdout(buf):
                rc = tr.main(["--file", str(path), "stats", "--jq", "attempt_count"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue()), 3)
            err = StringIO()
            with redirect_stderr(err):
                rc = tr.main(["--file", str(path), "stats", "--jq", "nope"])
            self.assertEqual(rc, 2)
            self.assertIn("bad --jq key", err.getvalue())

    def test_export_jq_prints_nested_field(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.save({"plan": "p", "attempt_count": 4}, path)
            buf = StringIO()
            with redirect_stdout(buf):
                rc = tr.main(["--file", str(path), "export", "--jq", "attempt_count"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue()), 4)

    def test_stats_out_writes_file(self) -> None:
        from io import StringIO
        from contextlib import redirect_stderr

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            out_path = Path(tmp) / "stats.json"
            tr.save({"plan": "p", "attempt_count": 2}, path)
            err = StringIO()
            with redirect_stderr(err):
                rc = tr.main(["--file", str(path), "stats", "--out", str(out_path)])
            self.assertEqual(rc, 0)
            out = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(out["attempt_count"], 2)
            self.assertIn("wrote", err.getvalue())

    def test_show_out_writes_file(self) -> None:
        from io import StringIO
        from contextlib import redirect_stderr

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            out_path = Path(tmp) / "show.json"
            tr.save({"plan": "p"}, path)
            err = StringIO()
            with redirect_stderr(err):
                rc = tr.main(["--file", str(path), "show", "--out", str(out_path)])
            self.assertEqual(rc, 0)
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["trace"]["plan"], "p")
            self.assertTrue(payload["exists"])
            self.assertIn("wrote", err.getvalue())

    def test_record_note_dash_reads_utf8_stdin(self) -> None:
        """`record --note -` decodes stdin as UTF-8 even on a cp1252 console —
        fix_stdio now covers stdin, so non-ASCII notes are not mojibake."""
        import subprocess

        script = ROOT / "skills" / "jev-consult" / "scripts" / "trace.py"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--file",
                    str(path),
                    "record",
                    "--pick",
                    "a",
                    "--note",
                    "-",
                ],
                input="héllo ünïcode ☃".encode("utf-8"),
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[:200])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["notes"][-1]["text"], "héllo ünïcode ☃")

    def test_record_note_stamps_sha(self) -> None:
        import hashlib
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            from io import StringIO
            from contextlib import redirect_stdout

            with redirect_stdout(StringIO()):
                code = tr.main(["--file", str(path), "record", "--note", "first", "--pick", "a"])
                self.assertEqual(code, 0)
                tr.main(["--file", str(path), "record", "--note", "first", "--pick", "b"])
            data = tr.load(path)
            notes = data["notes"]
            want = hashlib.sha256("first".encode("utf-8")).hexdigest()[:12]
            self.assertEqual(notes[0]["sha"], want)
            self.assertEqual(notes[1]["sha"], want)

    def test_history_grep_filters_picks(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            data = tr.empty()
            data = tr.record(data, pick="scaffold", kind="approach")
            data = tr.record(data, pick="jwt-auth", kind="skill")
            tr.save(data, path)
            buf = StringIO()
            with redirect_stdout(buf):
                code = tr.main(["--file", str(path), "history", "--grep", "jwt"])
            self.assertEqual(code, 0)
            out = buf.getvalue()
            self.assertIn("jwt-auth", out)
            self.assertNotIn("scaffold", out)
            buf = StringIO()
            with patch.dict(os.environ, {"JEV_TRACE_HISTORY_GREP": "approach"}):
                with redirect_stdout(buf):
                    tr.main(["--file", str(path), "history"])
            self.assertIn("scaffold", buf.getvalue())
            self.assertNotIn("jwt-auth", buf.getvalue())

    def test_history_kinds_lists_distinct_with_counts(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            data = tr.empty()
            data = tr.record(data, pick="a", kind="idf")
            data = tr.record(data, pick="b", kind="idf")
            data = tr.record(data, pick="c", kind="explicit")
            data = tr.record(data, pick="d")  # no kind
            tr.save(data, path)
            buf = StringIO()
            with redirect_stdout(buf):
                code = tr.main(["--file", str(path), "history", "--kinds"])
            self.assertEqual(code, 0)
            self.assertEqual(buf.getvalue().splitlines(), ["idf 2", "- 1", "explicit 1"])
            buf = StringIO()
            with redirect_stdout(buf):
                code = tr.main(["--file", str(path), "history", "--kinds", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(
                json.loads(buf.getvalue())["kinds"],
                {"idf": 2, "": 1, "explicit": 1},
            )

    def _write_history(self, path, stamps):
        data = tr.empty()
        for i, ts in enumerate(stamps):
            data["history"].append(
                {
                    "pick": "p%d" % i,
                    "ts": ts,
                    "kind": "idf" if i % 2 == 0 else "explicit",
                }
            )
        tr.save(data, path)

    def test_history_rate_json_buckets_and_rate(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout
        import datetime as _dt

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            day0 = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc).timestamp()
            day1 = day0 + 86400
            self._write_history(path, [day0, day0 + 3600, day1])
            buf = StringIO()
            with redirect_stdout(buf):
                code = tr.main(
                    ["--file", str(path), "history", "--rate", "--json"]
                )
            self.assertEqual(code, 0)
            rate = json.loads(buf.getvalue())["rate"]
            self.assertEqual(rate["count"], 3)
            self.assertEqual(rate["stamped"], 3)
            self.assertEqual(rate["first_ts"], day0)
            self.assertEqual(rate["last_ts"], day1)
            self.assertEqual(rate["days"], 2)
            self.assertEqual(rate["picks_per_day"], 1.5)
            self.assertEqual(
                rate["per_day"], {"2026-01-01": 2, "2026-01-02": 1}
            )

    def test_history_rate_text_rows(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            self._write_history(path, [1000.0, 2000.0])
            buf = StringIO()
            with redirect_stdout(buf):
                code = tr.main(["--file", str(path), "history", "--rate"])
            self.assertEqual(code, 0)
            lines = buf.getvalue().splitlines()
            self.assertIn("count 2", lines)
            self.assertIn("days 1", lines)
            self.assertIn("picks_per_day 2.0", lines)
            self.assertTrue(any(l.startswith("1970-01-01 2") for l in lines))

    def test_history_gap_lists_wide_gaps(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            self._write_history(path, [100.0, 110.0, 500.0, 510.0])
            buf = StringIO()
            with redirect_stdout(buf):
                code = tr.main(
                    ["--file", str(path), "history", "--gap", "50", "--json"]
                )
            self.assertEqual(code, 0)
            gaps = json.loads(buf.getvalue())["gaps"]
            self.assertEqual(len(gaps), 1)
            self.assertEqual(gaps[0]["gap_s"], 390.0)
            self.assertEqual(gaps[0]["prev_pick"], "p1")
            self.assertEqual(gaps[0]["pick"], "p2")

    def test_history_gap_empty_when_all_narrow(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            self._write_history(path, [100.0, 110.0, 120.0])
            buf = StringIO()
            with redirect_stdout(buf):
                code = tr.main(
                    ["--file", str(path), "history", "--gap", "60", "--json"]
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buf.getvalue())["gaps"], [])

    def test_history_rate_empty_history(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.save(tr.empty(), path)
            buf = StringIO()
            with redirect_stdout(buf):
                code = tr.main(
                    ["--file", str(path), "history", "--rate", "--json"]
                )
            self.assertEqual(code, 0)
            rate = json.loads(buf.getvalue())["rate"]
            self.assertEqual(rate["count"], 0)
            self.assertEqual(rate["stamped"], 0)
            self.assertIsNone(rate["first_ts"])
            self.assertEqual(rate["days"], 0)
            self.assertEqual(rate["picks_per_day"], 0.0)
            self.assertEqual(rate["per_day"], {})

    def test_history_rate_honors_since_window(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            self._write_history(path, [100.0, 200.0, 300.0])
            buf = StringIO()
            with redirect_stdout(buf):
                code = tr.main(
                    [
                        "--file",
                        str(path),
                        "history",
                        "--rate",
                        "--json",
                        "--since",
                        "150",
                    ]
                )
            self.assertEqual(code, 0)
            rate = json.loads(buf.getvalue())["rate"]
            self.assertEqual(rate["count"], 2)
            self.assertEqual(rate["first_ts"], 200.0)

    def test_notes_grep_filters_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            from io import StringIO
            from contextlib import redirect_stdout

            with redirect_stdout(StringIO()):
                tr.main(["--file", str(path), "record", "--pick", "a", "--note", "jwt tokens"])
                tr.main(["--file", str(path), "record", "--pick", "b", "--note", "unrelated"])
            buf = StringIO()
            with redirect_stdout(buf):
                code = tr.main(["--file", str(path), "notes", "--grep", "JWT"])
            self.assertEqual(code, 0)
            out = buf.getvalue()
            self.assertIn("jwt tokens", out)
            self.assertNotIn("unrelated", out)
            buf = StringIO()
            with patch.dict(os.environ, {"JEV_TRACE_GREP": "unrel"}):
                with redirect_stdout(buf):
                    tr.main(["--file", str(path), "notes"])
            self.assertNotIn("jwt tokens", buf.getvalue())
            self.assertIn("unrelated", buf.getvalue())

    def test_notes_uniq_dedupes_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            from io import StringIO
            from contextlib import redirect_stdout

            with redirect_stdout(StringIO()):
                tr.main(["--file", str(path), "record", "--pick", "a", "--note", "same note"])
                tr.main(["--file", str(path), "record", "--pick", "b", "--note", "same note"])
                tr.main(["--file", str(path), "record", "--pick", "c", "--note", "different"])
            buf = StringIO()
            with redirect_stdout(buf):
                code = tr.main(["--file", str(path), "notes", "--uniq", "--json"])
            self.assertEqual(code, 0)
            notes = json.loads(buf.getvalue())
            self.assertEqual(len(notes), 2)

    def test_notes_reverse_lists_newest_first(self) -> None:
        import time
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            now = time.time()
            tr.save(
                {"notes": [{"ts": now - 10, "text": "old"}, {"ts": now, "text": "new"}]},
                path,
            )
            buf = StringIO()
            with redirect_stdout(buf):
                rc = tr.main(["--file", str(path), "notes", "--json", "--reverse"])
            self.assertEqual(rc, 0)
            notes = json.loads(buf.getvalue())
            self.assertEqual([n["text"] for n in notes], ["new", "old"])

    def test_history_reverse_lists_newest_first(self) -> None:
        import time
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            now = time.time()
            tr.save(
                {
                    "history": [
                        {"pick": "a", "ts": now - 10, "kind": "x"},
                        {"pick": "b", "ts": now, "kind": "x"},
                    ]
                },
                path,
            )
            buf = StringIO()
            with redirect_stdout(buf):
                rc = tr.main(["--file", str(path), "history", "--json", "--reverse"])
            self.assertEqual(rc, 0)
            picks = json.loads(buf.getvalue())
            self.assertEqual([h["pick"] for h in picks], ["b", "a"])

    def test_prune_dry_run_reports_without_deleting(self) -> None:
        import os
        import time
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.save({"plan": "p"}, path)
            old = time.time() - 3600
            os.utime(path, (old, old))
            buf = StringIO()
            with redirect_stdout(buf):
                rc = tr.main(["--file", str(path), "prune", "--older-than", "60", "--dry-run"])
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertFalse(out["removed"])
            self.assertTrue(out["would_remove"])
            self.assertTrue(path.is_file())

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

    def test_cli_notes_edit_rewrites_text_and_sha(self) -> None:
        import hashlib as _hl

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.main(["--file", str(path), "init", "--plan", "P"])
            for text in ("a", "b", "c"):
                tr.main(["--file", str(path), "record", "--pick", "x", "--note", text])
            before = json.loads(path.read_text(encoding="utf-8"))["notes"]
            rc = tr.main(["--file", str(path), "notes", "--edit", "2", "B2"])
            self.assertEqual(rc, 0)
            after = json.loads(path.read_text(encoding="utf-8"))["notes"]
            self.assertEqual([n["text"] for n in after], ["a", "B2", "c"])
            self.assertEqual(after[1]["ts"], before[1]["ts"])
            self.assertEqual(
                after[1]["sha"],
                _hl.sha256("B2".encode("utf-8")).hexdigest()[:12],
            )
            self.assertNotEqual(after[1]["sha"], before[1]["sha"])

    def test_cli_notes_edit_out_of_range_rc2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.main(["--file", str(path), "init", "--plan", "P"])
            tr.main(["--file", str(path), "record", "--pick", "x", "--note", "a"])
            self.assertEqual(
                tr.main(["--file", str(path), "notes", "--edit", "5", "x"]), 2
            )
            self.assertEqual(
                tr.main(["--file", str(path), "notes", "--edit", "x", "y"]), 2
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([n["text"] for n in data["notes"]], ["a"])

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

    def test_state_watch_emits_ticks(self) -> None:
        import io
        import os as _os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(
                json.dumps(
                    {
                        "plan": "P",
                        "attempt_count": 2,
                        "history": [{"ts": 1, "pick": "a"}],
                        "inspected": [{"name": "x"}],
                    }
                ),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch.dict(_os.environ, {"JEV_TRACE_WATCH_MAX": "2"}):
                with patch.object(sys, "stdout", buf):
                    rc = tr.main(
                        ["--file", str(path), "state", "--watch", "0.01"]
                    )
            self.assertEqual(rc, 0)
            ticks = [
                json.loads(l)
                for l in buf.getvalue().splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 2)
            self.assertEqual(ticks[0]["state"]["plan"], "P")
            self.assertEqual(ticks[0]["attempt_count"], 2)
            self.assertEqual(ticks[0]["history"], 1)
            self.assertEqual(ticks[0]["inspected"], 1)

    def test_state_watch_tick_reports_attempt_count_delta(self) -> None:
        import io
        import os as _os
        from unittest.mock import patch

        loads = [
            {"plan": "P", "attempt_count": 2},
            {"plan": "P", "attempt_count": 5},
        ]

        def fake_load(path):
            return loads.pop(0) if loads else {"plan": "P", "attempt_count": 5}

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text('{"plan": "P", "attempt_count": 2}\n', encoding="utf-8")
            buf = io.StringIO()
            with patch.dict(_os.environ, {"JEV_TRACE_WATCH_MAX": "2"}):
                with patch.object(tr, "load", side_effect=fake_load):
                    with patch.object(sys, "stdout", buf):
                        rc = tr.main(
                            ["--file", str(path), "state", "--watch", "0.01"]
                        )
            self.assertEqual(rc, 0)
            ticks = [
                json.loads(l)
                for l in buf.getvalue().splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 2)
            self.assertIsNone(ticks[0]["attempt_count_delta"])
            self.assertEqual(ticks[1]["attempt_count_delta"], 3)

    def test_state_watch_tick_reports_elapsed_s(self) -> None:
        import io
        import os as _os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text('{"plan": "P", "attempt_count": 2}\n', encoding="utf-8")
            buf = io.StringIO()
            with patch.dict(_os.environ, {"JEV_TRACE_WATCH_MAX": "2"}):
                with patch.object(sys, "stdout", buf):
                    rc = tr.main(
                        ["--file", str(path), "state", "--watch", "0.01"]
                    )
            self.assertEqual(rc, 0)
            ticks = [
                json.loads(l)
                for l in buf.getvalue().splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 2)
            self.assertTrue(all(isinstance(t["elapsed_s"], float) for t in ticks))
            self.assertGreaterEqual(ticks[1]["elapsed_s"], ticks[0]["elapsed_s"])

    def test_stats_watch_tick_reports_elapsed_s(self) -> None:
        import io
        import os as _os

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text('{"plan": "P"}\n', encoding="utf-8")
            buf = io.StringIO()
            with patch.dict(_os.environ, {"JEV_TRACE_WATCH_MAX": "2"}):
                with patch.object(sys, "stdout", buf):
                    rc = tr.main(
                        ["--file", str(path), "stats", "--watch", "0.01"]
                    )
            self.assertEqual(rc, 0)
            ticks = [
                json.loads(l)
                for l in buf.getvalue().splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 2)
            self.assertTrue(all(isinstance(t["elapsed_s"], float) for t in ticks))

    def test_state_watch_fail_fast_breaks_on_empty(self) -> None:
        import io
        import os as _os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text('{"attempt_count": 1}\n', encoding="utf-8")
            buf = io.StringIO()
            with patch.dict(_os.environ, {"JEV_TRACE_WATCH_MAX": "9"}):
                with patch.object(sys, "stdout", buf):
                    rc = tr.main(
                        ["--file", str(path), "state", "--watch", "0.01",
                         "--fail-fast"]
                    )
            self.assertEqual(rc, 1)
            ticks = [
                json.loads(l)
                for l in buf.getvalue().splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 1)

    def test_stats_watch_fail_fast_breaks_when_missing(self) -> None:
        import io
        import os as _os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nope.json"
            buf = io.StringIO()
            with patch.dict(_os.environ, {"JEV_TRACE_WATCH_MAX": "9"}):
                with patch.object(sys, "stdout", buf):
                    rc = tr.main(
                        ["--file", str(path), "stats", "--watch", "0.01",
                         "--fail-fast"]
                    )
            self.assertEqual(rc, 1)
            ticks = [
                json.loads(l)
                for l in buf.getvalue().splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 1)
            self.assertFalse(ticks[0]["exists"])

    def test_state_watch_verdict_writes_final_state(self) -> None:
        import io
        import os as _os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(
                json.dumps({"plan": "P", "attempt_count": 2, "history": [{"ts": 1, "pick": "a"}]}),
                encoding="utf-8",
            )
            verdict = Path(tmp) / "v.json"
            with patch.dict(_os.environ, {"JEV_TRACE_WATCH_MAX": "2"}):
                with patch.object(sys, "stdout", io.StringIO()):
                    rc = tr.main(
                        ["--file", str(path), "state", "--watch", "0.01",
                         "--verdict", str(verdict)]
                    )
            self.assertEqual(rc, 0)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "ok")
            self.assertEqual(payload["ticks"], 2)
            self.assertEqual(payload["attempt_count"], 2)
            self.assertEqual(payload["state"]["plan"], "P")

    def test_state_watch_verdict_empty(self) -> None:
        import io
        import os as _os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(json.dumps({"attempt_count": 3}), encoding="utf-8")
            verdict = Path(tmp) / "v.json"
            with patch.dict(_os.environ, {"JEV_TRACE_WATCH_MAX": "1"}):
                with patch.object(sys, "stdout", io.StringIO()):
                    rc = tr.main(
                        ["--file", str(path), "state", "--watch", "0.01",
                         "--verdict", str(verdict)]
                    )
            self.assertEqual(rc, 1)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "empty")

    def test_notes_watch_emits_count_ticks(self) -> None:
        import io
        import os as _os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(
                json.dumps({"notes": [{"ts": 1, "text": "a"}, {"ts": 2, "text": "b"}]}),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch.dict(_os.environ, {"JEV_TRACE_WATCH_MAX": "2"}):
                with patch.object(sys, "stdout", buf):
                    rc = tr.main(
                        ["--file", str(path), "notes", "--watch", "0.01"]
                    )
            self.assertEqual(rc, 0)
            ticks = [
                json.loads(l)
                for l in buf.getvalue().splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 2)
            self.assertTrue(all(t["notes"] == 2 for t in ticks))

    def test_notes_watch_verdict_writes_notes_count(self) -> None:
        import io
        import os as _os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(
                json.dumps({"notes": [{"ts": 1, "text": "a"}, {"ts": 2, "text": "b"}]}),
                encoding="utf-8",
            )
            verdict = Path(tmp) / "v.json"
            buf = io.StringIO()
            with patch.dict(_os.environ, {"JEV_TRACE_WATCH_MAX": "2"}):
                with patch.object(sys, "stdout", buf):
                    rc = tr.main(
                        ["--file", str(path), "notes", "--watch", "0.01", "--verdict", str(verdict)]
                    )
            self.assertEqual(rc, 0)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "notes")
            self.assertEqual(payload["ticks"], 2)
            self.assertEqual(payload["notes"], 2)

    def test_export_dumps_bundle(self) -> None:
        import io

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(
                json.dumps(
                    {
                        "history": [{"ts": 1, "kind": "skill", "pick": "a"}],
                        "notes": [{"ts": 1, "text": "n"}],
                    }
                ),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = tr.main(["--file", str(path), "export"])
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertEqual(out["history"][0]["pick"], "a")
            self.assertEqual(out["notes"][0]["text"], "n")
            self.assertEqual(out["file"], str(path))

    def test_export_out_writes_file(self) -> None:
        import io

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(json.dumps({"history": []}), encoding="utf-8")
            out_path = Path(tmp) / "bundle.json"
            with patch.object(sys, "stdout", io.StringIO()):
                rc = tr.main(
                    ["--file", str(path), "export", "--out", str(out_path)]
                )
            self.assertEqual(rc, 0)
            self.assertIn("history", json.loads(out_path.read_text(encoding="utf-8")))

    def test_export_csv_emits_history_rows(self) -> None:
        import io
        import csv as _csv

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            self._write_history(path, [1000.0, 2000.0])
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = tr.main(["--file", str(path), "export", "--csv"])
            self.assertEqual(rc, 0)
            rows = list(_csv.reader(io.StringIO(buf.getvalue())))
            self.assertEqual(rows[0], ["pick", "ts", "iso", "kind"])
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[1][0], "p0")
            self.assertEqual(rows[2][0], "p1")
            self.assertEqual(rows[1][3], "idf")
            self.assertEqual(rows[2][3], "explicit")

    def test_export_csv_honors_kinds_and_out(self) -> None:
        import io

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            self._write_history(path, [1000.0, 2000.0])
            out_path = Path(tmp) / "hist.csv"
            with patch.object(sys, "stdout", io.StringIO()):
                rc = tr.main(
                    [
                        "--file", str(path), "export",
                        "--csv", "--kinds", "explicit",
                        "--out", str(out_path),
                    ]
                )
            self.assertEqual(rc, 0)
            lines = out_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn("p1", lines[1])
            self.assertNotIn("p0", lines[1])

    def test_export_since_before_bounds_history_and_notes(self) -> None:
        import io

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(
                json.dumps(
                    {
                        "history": [
                            {"ts": 10, "pick": "old"},
                            {"ts": 20, "pick": "mid"},
                            {"ts": 30, "pick": "new"},
                        ],
                        "notes": [{"ts": 10, "text": "a"}, {"ts": 25, "text": "b"}],
                        "inspected": [{"name": "untimed"}],
                    }
                ),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = tr.main(
                    ["--file", str(path), "export", "--since", "15", "--before", "25"]
                )
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertEqual([h["pick"] for h in out["history"]], ["mid"])
            self.assertEqual([n["text"] for n in out["notes"]], ["b"])
            self.assertEqual(out["inspected"], [{"name": "untimed"}])

    def test_export_kinds_filters_history_only(self) -> None:
        import io

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(
                json.dumps(
                    {
                        "history": [
                            {"pick": "a", "ts": 1, "kind": "explicit"},
                            {"pick": "b", "ts": 2, "kind": "idf"},
                            {"pick": "c", "ts": 3},
                        ],
                        "notes": [{"ts": 1, "text": "n"}],
                    }
                ),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = tr.main(
                    ["--file", str(path), "export", "--kinds", "explicit,idf"]
                )
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertEqual([h["pick"] for h in out["history"]], ["a", "b"])
            self.assertEqual(len(out["notes"]), 1)

    def test_export_since_bad_value_rc2(self) -> None:
        import io

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(json.dumps({"history": []}), encoding="utf-8")
            with patch.object(sys, "stdout", io.StringIO()), patch.object(
                sys, "stderr", io.StringIO()
            ) as err:
                rc = tr.main(["--file", str(path), "export", "--since", "bogus"])
            self.assertEqual(rc, 2)
            self.assertIn("bad --since", err.getvalue())

    def test_history_watch_verdict_writes_pick_count(self) -> None:
        import io
        import os as _os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(
                json.dumps({"history": [{"ts": 1, "kind": "skill", "pick": "a"}]}),
                encoding="utf-8",
            )
            verdict = Path(tmp) / "v.json"
            buf = io.StringIO()
            with patch.dict(_os.environ, {"JEV_TRACE_WATCH_MAX": "1"}):
                with patch.object(sys, "stdout", buf):
                    rc = tr.main(
                        ["--file", str(path), "history", "--watch", "0.01", "--verdict", str(verdict)]
                    )
            self.assertEqual(rc, 0)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "picks")
            self.assertEqual(payload["picks"], 1)

    def test_history_watch_fail_fast_stops_on_zero_picks(self) -> None:
        import io
        import os as _os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(json.dumps({"history": []}), encoding="utf-8")
            buf = io.StringIO()
            with patch.dict(_os.environ, {"JEV_TRACE_WATCH_MAX": "5"}):
                with patch.object(sys, "stdout", buf):
                    rc = tr.main(
                        ["--file", str(path), "history", "--watch", "0.01", "--fail-fast"]
                    )
            self.assertEqual(rc, 0)
            ticks = [
                json.loads(l) for l in buf.getvalue().splitlines() if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 1)
            self.assertEqual(ticks[0]["picks"], 0)

    def test_state_watch_writes_stderr_tick_summary(self) -> None:
        import io
        import os as _os

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(
                json.dumps({"plan": "x", "attempt_count": 2}),
                encoding="utf-8",
            )
            err = io.StringIO()
            with patch.dict(_os.environ, {"JEV_TRACE_WATCH_MAX": "2"}):
                with patch.object(sys, "stdout", io.StringIO()):
                    with patch.object(sys, "stderr", err):
                        rc = tr.main(
                            ["--file", str(path), "state", "--watch", "0.01"]
                        )
            self.assertEqual(rc, 0)
            lines = [
                l for l in err.getvalue().splitlines() if l.startswith("watch tick=")
            ]
            self.assertEqual(len(lines), 2)
            self.assertIn("attempt_count=2", lines[0])

    def test_notes_watch_fail_fast_stops_on_empty(self) -> None:
        import io
        import os as _os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(json.dumps({"notes": []}), encoding="utf-8")
            buf = io.StringIO()
            with patch.dict(_os.environ, {"JEV_TRACE_WATCH_MAX": "5"}):
                with patch.object(sys, "stdout", buf):
                    rc = tr.main(
                        ["--file", str(path), "notes", "--watch", "0.01", "--fail-fast"]
                    )
            self.assertEqual(rc, 0)
            ticks = [
                json.loads(l) for l in buf.getvalue().splitlines() if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 1)
            self.assertEqual(ticks[0]["notes"], 0)

    def test_stats_watch_verdict_writes_state(self) -> None:
        import io
        import os as _os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(
                json.dumps({"attempt_count": 2, "history": [{"ts": 1, "pick": "a"}]}),
                encoding="utf-8",
            )
            verdict = Path(tmp) / "v.json"
            buf = io.StringIO()
            with patch.dict(_os.environ, {"JEV_TRACE_WATCH_MAX": "2"}):
                with patch.object(sys, "stdout", buf):
                    rc = tr.main(
                        ["--file", str(path), "stats", "--watch", "0.01", "--verdict", str(verdict)]
                    )
            self.assertEqual(rc, 0)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "exists")
            self.assertEqual(payload["ticks"], 2)
            self.assertEqual(payload["attempt_count"], 2)
            self.assertEqual(payload["history"], 1)

    def test_history_watch_emits_pick_ticks(self) -> None:
        import io
        import os as _os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(
                json.dumps(
                    {"history": [{"ts": 1, "kind": "skill", "pick": "a"},
                                 {"ts": 2, "kind": "skill", "pick": "b"}]}
                ),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch.dict(_os.environ, {"JEV_TRACE_WATCH_MAX": "2"}):
                with patch.object(sys, "stdout", buf):
                    rc = tr.main(
                        ["--file", str(path), "history", "--watch", "0.01"]
                    )
            self.assertEqual(rc, 0)
            ticks = [
                json.loads(l)
                for l in buf.getvalue().splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 2)
            self.assertTrue(all(t["picks"] == 2 for t in ticks))

    def test_stats_watch_emits_ticks(self) -> None:
        import io
        import os as _os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(
                json.dumps({"attempt_count": 2, "history": [{"ts": 1, "pick": "a"}]}),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch.dict(_os.environ, {"JEV_TRACE_WATCH_MAX": "2"}):
                with patch.object(sys, "stdout", buf):
                    rc = tr.main(
                        ["--file", str(path), "stats", "--watch", "0.01"]
                    )
            self.assertEqual(rc, 0)
            ticks = [
                json.loads(l)
                for l in buf.getvalue().splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 2)
            self.assertTrue(all(t["attempt_count"] == 2 for t in ticks))
            self.assertTrue(all(t["history"] == 1 for t in ticks))

    def test_stats_watch_appends_ticks_to_out_file(self) -> None:
        import io
        import os as _os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(
                json.dumps({"attempt_count": 2, "history": [{"ts": 1, "pick": "a"}]}),
                encoding="utf-8",
            )
            out = Path(tmp) / "ticks.jsonl"
            buf = io.StringIO()
            with patch.dict(_os.environ, {"JEV_TRACE_WATCH_MAX": "2"}):
                with patch.object(sys, "stdout", buf):
                    rc = tr.main(
                        ["--file", str(path), "stats", "--watch", "0.01",
                         "--out", str(out)]
                    )
            self.assertEqual(rc, 0)
            lines = [
                json.loads(l)
                for l in out.read_text(encoding="utf-8").splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(lines), 2)
            self.assertTrue(all("exists" in t for t in lines))

    def test_state_watch_rc_1_when_state_empty(self) -> None:
        import io
        import os as _os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text("{}", encoding="utf-8")
            with patch.dict(_os.environ, {"JEV_TRACE_WATCH_MAX": "1"}):
                with patch.object(sys, "stdout", io.StringIO()):
                    rc = tr.main(
                        ["--file", str(path), "state", "--watch", "0.01"]
                    )
            self.assertEqual(rc, 1)

    def test_stats_watch_rc_1_when_file_missing(self) -> None:
        import io
        import os as _os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nope.json"
            with patch.dict(_os.environ, {"JEV_TRACE_WATCH_MAX": "1"}):
                with patch.object(sys, "stdout", io.StringIO()):
                    rc = tr.main(
                        ["--file", str(path), "stats", "--watch", "0.01"]
                    )
            self.assertEqual(rc, 1)

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


class WatchJqTests(unittest.TestCase):
    def _trace(self, tmp: str) -> Path:
        path = Path(tmp) / "trace.json"
        path.write_text(
            json.dumps(
                {
                    "plan": "P",
                    "attempt_count": 2,
                    "history": [{"ts": 1, "pick": "a"}],
                    "inspected": [{"name": "x"}],
                    "notes": [{"ts": 1, "text": "n"}],
                }
            ),
            encoding="utf-8",
        )
        return path

    def _watch_jq(self, path: Path, cmd: str, key: str):
        import io
        import os as _os

        buf = io.StringIO()
        with patch.dict(_os.environ, {"JEV_TRACE_WATCH_MAX": "2"}):
            with patch.object(sys, "stdout", buf):
                rc = tr.main(
                    ["--file", str(path), cmd, "--watch", "0.01", "--jq", key]
                )
        return rc, buf.getvalue().splitlines()

    def test_state_watch_jq_prints_only_named_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, lines = self._watch_jq(self._trace(tmp), "state", "attempt_count")
        self.assertEqual(rc, 0)
        self.assertEqual(lines, ["2", "2"])

    def test_history_watch_jq_prints_only_named_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, lines = self._watch_jq(self._trace(tmp), "history", "picks")
        self.assertEqual(rc, 0)
        self.assertEqual(lines, ["1", "1"])

    def test_notes_watch_jq_prints_only_named_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, lines = self._watch_jq(self._trace(tmp), "notes", "notes")
        self.assertEqual(rc, 0)
        self.assertEqual(lines, ["1", "1"])

    def test_stats_watch_jq_prints_only_named_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, lines = self._watch_jq(self._trace(tmp), "stats", "exists")
        self.assertEqual(rc, 0)
        self.assertEqual(lines, ["true", "true"])

class WatchQuietEnvTests(unittest.TestCase):
    def test_stats_watch_quiet_env_suppresses_clean_ticks(self) -> None:
        import io
        import os as _os

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            path.write_text(json.dumps({"attempt_count": 1}), encoding="utf-8")
            buf = io.StringIO()
            err = io.StringIO()
            with patch.dict(
                _os.environ,
                {"JEV_TRACE_WATCH_MAX": "2", "JEV_TRACE_WATCH_QUIET": "1"},
            ):
                with patch.object(sys, "stdout", buf), patch.object(
                    sys, "stderr", err
                ):
                    rc = tr.main(
                        ["--file", str(path), "stats", "--watch", "0.01"]
                    )
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue(), "")
            self.assertIn("watch tick=1", err.getvalue())

class VerdictOneshotTests(unittest.TestCase):
    def _trace(self, tmp: str) -> Path:
        path = Path(tmp) / "trace.json"
        path.write_text(
            json.dumps(
                {
                    "plan": "P",
                    "attempt_count": 2,
                    "history": [{"ts": 1, "pick": "a"}],
                    "inspected": [{"name": "x"}],
                    "notes": [{"ts": 1, "text": "n"}],
                }
            ),
            encoding="utf-8",
        )
        return path

    def _run(self, path: Path, cmd: str, verdict: Path):
        import io

        with patch.object(sys, "stdout", io.StringIO()):
            rc = tr.main(["--file", str(path), cmd, "--verdict", str(verdict)])
        return rc, json.loads(verdict.read_text(encoding="utf-8"))

    def test_state_verdict_ok_when_state_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, payload = self._run(
                self._trace(tmp), "state", Path(tmp) / "v.json"
            )
        self.assertEqual(rc, 0)
        self.assertEqual(payload["verdict"], "ok")
        self.assertEqual(payload["ticks"], 1)
        self.assertEqual(payload["attempt_count"], 2)
        self.assertEqual(payload["state"]["plan"], "P")

    def test_state_verdict_empty_without_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            rc, payload = self._run(missing, "state", Path(tmp) / "v.json")
        self.assertEqual(rc, 0)
        self.assertEqual(payload["verdict"], "empty")
        self.assertEqual(payload["attempt_count"], 0)

    def test_stats_verdict_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, payload = self._run(
                self._trace(tmp), "stats", Path(tmp) / "v.json"
            )
        self.assertEqual(rc, 0)
        self.assertEqual(payload["verdict"], "exists")
        self.assertEqual(payload["history"], 1)
        self.assertEqual(payload["inspected"], 1)

    def test_stats_verdict_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, payload = self._run(
                Path(tmp) / "nope.json", "stats", Path(tmp) / "v.json"
            )
        self.assertEqual(rc, 0)
        self.assertEqual(payload["verdict"], "missing")
        self.assertEqual(payload["ticks"], 1)

    def test_notes_verdict_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, payload = self._run(
                self._trace(tmp), "notes", Path(tmp) / "v.json"
            )
        self.assertEqual(rc, 0)
        self.assertEqual(payload["verdict"], "notes")
        self.assertEqual(payload["notes"], 1)

    def test_history_verdict_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, payload = self._run(
                self._trace(tmp), "history", Path(tmp) / "v.json"
            )
        self.assertEqual(rc, 0)
        self.assertEqual(payload["verdict"], "picks")
        self.assertEqual(payload["picks"], 1)

class WatchSecsEnvTests(unittest.TestCase):
    def test_watch_secs_env_bounds_loop(self) -> None:
        import io
        import os as _os
        import time as _time
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_text(
                json.dumps({"plan": "P", "attempt_count": 1}),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch.dict(
                _os.environ,
                {"JEV_TRACE_WATCH_MAX": "0", "JEV_TRACE_WATCH_SECS": "0.05"},
            ):
                start = _time.time()
                with patch.object(sys, "stdout", buf):
                    rc = tr.main(
                        ["--file", str(path), "state", "--watch", "0.02"]
                    )
            self.assertEqual(rc, 0)
            self.assertLess(_time.time() - start, 2.0)
            ticks = [
                l for l in buf.getvalue().splitlines() if l.startswith("{")
            ]
            self.assertLessEqual(len(ticks), 10)
            self.assertGreaterEqual(len(ticks), 1)

class AtomicWriteTests(unittest.TestCase):
    def test_save_leaves_no_tmp_and_roundtrips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.save({"plan": "p", "attempt_count": 3}, path)
            self.assertEqual(
                [f.name for f in Path(tmp).iterdir()], ["trace.json"]
            )
            data = tr.load(path)
            self.assertEqual(data["plan"], "p")
            self.assertEqual(data["attempt_count"], 3)

    def test_save_replace_failure_leaves_no_tmp(self) -> None:
        from unittest.mock import patch as _patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            with _patch.object(tr.os, "replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    tr.save({"plan": "p"}, path)
            self.assertFalse(path.exists())
            self.assertFalse((Path(tmp) / "trace.json.tmp").exists())

    def test_suggest_dry_run_uses_next_move_template(self) -> None:
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.save({"plan": "p", "current_step": "s2"}, path)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tr.main(
                    ["--file", str(path), "suggest", "--dry-run"]
                )
            self.assertEqual(rc, 0)
            req = json.loads(buf.getvalue())
            q = req["questions"]["next_move"]
            self.assertIn("return_to_plan", q["criteria"])
            self.assertEqual(req["state"]["plan"], "p")
            # dry-run records nothing
            self.assertFalse(tr.load(path)["history"])

    def test_suggest_pick_records_history(self) -> None:
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.save({"plan": "p"}, path)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tr.main(
                    [
                        "--file",
                        str(path),
                        "suggest",
                        "--pick",
                        "ask_human",
                        "--kind",
                        "suggest",
                    ]
                )
            self.assertEqual(rc, 0)
            data = tr.load(path)
            self.assertEqual(data["last_pick"], "ask_human")
            self.assertEqual(data["history"][-1]["pick"], "ask_human")
            self.assertEqual(data["history"][-1]["kind"], "suggest")

    def test_suggest_unknown_template_rc2(self) -> None:
        import io
        from contextlib import redirect_stderr

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            tr.save({"plan": "p"}, path)
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = tr.main(
                    [
                        "--file",
                        str(path),
                        "suggest",
                        "--template",
                        "no_such_template",
                    ]
                )
            self.assertEqual(rc, 2)
            self.assertIn("no criteria", buf.getvalue())

    def test_suggest_out_writes_request(self) -> None:
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            out_path = Path(tmp) / "req.json"
            tr.save({"plan": "p"}, path)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tr.main(
                    [
                        "--file",
                        str(path),
                        "suggest",
                        "--dry-run",
                        "--out",
                        str(out_path),
                    ]
                )
            self.assertEqual(rc, 0)
            req = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertIn("next_move", req["questions"])

    def test_schema_prints_key_contract(self) -> None:
        import io

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = tr.main(["schema"])
        self.assertEqual(rc, 0)
        self.assertIn("attempt_count: int", buf.getvalue())
        self.assertIn("history: list[pick]", buf.getvalue())
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = tr.main(["schema", "--json"])
        self.assertEqual(rc, 0)
        rows = json.loads(buf.getvalue())
        for key in tr.EMPTY:
            self.assertIn(key, rows)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(verbosity=2) else 1)

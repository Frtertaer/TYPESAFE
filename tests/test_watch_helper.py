#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for scripts/_watch.py shared watch-tick plumbing."""
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
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

spec = importlib.util.spec_from_file_location("_watch", SCRIPTS / "_watch.py")
watch = importlib.util.module_from_spec(spec)
sys.modules["_watch"] = watch
spec.loader.exec_module(watch)


class CapTests(unittest.TestCase):
    def test_missing_env_is_uncapped(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("JEV_X_WATCH_MAX", None)
            self.assertEqual(watch.cap("JEV_X_WATCH_MAX"), 0)

    def test_valid_and_invalid_and_negative(self) -> None:
        with patch.dict("os.environ", {"JEV_X_WATCH_MAX": "3"}):
            self.assertEqual(watch.cap("JEV_X_WATCH_MAX"), 3)
        with patch.dict("os.environ", {"JEV_X_WATCH_MAX": "nope"}):
            self.assertEqual(watch.cap("JEV_X_WATCH_MAX"), 0)
        with patch.dict("os.environ", {"JEV_X_WATCH_MAX": "-5"}):
            self.assertEqual(watch.cap("JEV_X_WATCH_MAX"), 0)

    def test_invalid_env_warns_on_stderr(self) -> None:
        buf = io.StringIO()
        with patch.dict("os.environ", {"JEV_X_WATCH_MAX": "nope"}):
            with patch.object(sys, "stderr", buf):
                self.assertEqual(watch.cap("JEV_X_WATCH_MAX"), 0)
        self.assertIn("bad JEV_X_WATCH_MAX", buf.getvalue())
        # empty env stays silent
        buf2 = io.StringIO()
        with patch.dict("os.environ", {"JEV_X_WATCH_MAX": ""}):
            with patch.object(sys, "stderr", buf2):
                self.assertEqual(watch.cap("JEV_X_WATCH_MAX"), 0)
        self.assertEqual(buf2.getvalue(), "")
        # override path stays silent on bad value
        buf3 = io.StringIO()
        with patch.object(sys, "stderr", buf3):
            self.assertEqual(watch.cap("JEV_X_WATCH_MAX", "junk"), 0)
        self.assertEqual(buf3.getvalue(), "")

    def test_override_wins_over_env(self) -> None:
        with patch.dict("os.environ", {"JEV_X_WATCH_MAX": "9"}):
            self.assertEqual(watch.cap("JEV_X_WATCH_MAX", 2), 2)
            self.assertEqual(watch.cap("JEV_X_WATCH_MAX", "3"), 3)
            self.assertEqual(watch.cap("JEV_X_WATCH_MAX", -4), 0)
            self.assertEqual(watch.cap("JEV_X_WATCH_MAX", "junk"), 0)
            self.assertEqual(watch.cap("JEV_X_WATCH_MAX", 0), 9)


class DeadlineTests(unittest.TestCase):
    def test_deadline_zero_means_unbounded(self) -> None:
        import os

        os.environ.pop("JEV_X_WATCH_SECS", None)
        self.assertEqual(watch.deadline("JEV_X_WATCH_SECS"), 0.0)
        self.assertEqual(watch.deadline("JEV_X_WATCH_SECS", None), 0.0)
        self.assertEqual(watch.deadline("JEV_X_WATCH_SECS", 0), 0.0)
        self.assertEqual(watch.deadline("JEV_X_WATCH_SECS", "junk"), 0.0)
        self.assertEqual(watch.deadline("JEV_X_WATCH_SECS", -2), 0.0)

    def test_deadline_is_now_plus_seconds(self) -> None:
        import time

        before = time.time()
        d = watch.deadline("JEV_X_WATCH_SECS", 10)
        self.assertTrue(before + 9.9 < d < before + 10.1)
        self.assertTrue(watch.deadline("JEV_X_WATCH_SECS", "5") > time.time())

    def test_deadline_reads_env_when_no_override(self) -> None:
        import os
        import time

        os.environ.pop("JEV_X_WATCH_SECS", None)
        self.assertEqual(watch.deadline("JEV_X_WATCH_SECS"), 0.0)
        with patch.dict("os.environ", {"JEV_X_WATCH_SECS": "10"}):
            self.assertTrue(watch.deadline("JEV_X_WATCH_SECS") > time.time())
            # override 0 means unset, so the env still applies
            self.assertTrue(watch.deadline("JEV_X_WATCH_SECS", 0) > time.time())
            # a positive flag value wins over the env
            d = watch.deadline("JEV_X_WATCH_SECS", 3)
            self.assertTrue(d < time.time() + 4)
        with patch.dict("os.environ", {"JEV_X_WATCH_SECS": "junk"}):
            self.assertEqual(watch.deadline("JEV_X_WATCH_SECS"), 0.0)


class EmitTests(unittest.TestCase):
    def test_prints_json_line(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            watch.emit({"ts": 1, "ok": True})
        self.assertEqual(buf.getvalue(), '{"ts": 1, "ok": true}\n')

    def test_appends_to_out_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ticks.jsonl"
            buf = io.StringIO()
            with redirect_stdout(buf):
                watch.emit({"a": 1}, out)
                watch.emit({"a": 2}, str(out))
            lines = out.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            for line, a in zip(lines, (1, 2)):
                payload = json.loads(line)
                self.assertEqual(payload["a"], a)
                self.assertIsInstance(payload["ts"], int)

    def test_injects_ts_when_absent(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            watch.emit({"ok": True})
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"])
        self.assertIsInstance(payload["ts"], int)

    def test_preserves_caller_ts(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            watch.emit({"ts": 42, "ok": True})
        self.assertEqual(json.loads(buf.getvalue()), {"ts": 42, "ok": True})

    def test_does_not_mutate_caller_dict(self) -> None:
        tick = {"ok": True}
        buf = io.StringIO()
        with redirect_stdout(buf):
            watch.emit(tick)
        self.assertNotIn("ts", tick)

    def test_bad_out_path_is_fail_open(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            watch.emit({"a": 1}, Path("nul\\bad") / "nope" / "x.jsonl")
        self.assertIn('"a": 1', buf.getvalue())

    def test_quiet_suppresses_clean_but_not_bad(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ticks.jsonl"
            buf = io.StringIO()
            with redirect_stdout(buf):
                watch.emit({"ok": True}, out, quiet=True, bad=False)
                watch.emit({"ok": False}, out, quiet=True, bad=True)
            lines = buf.getvalue().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn('"ok": false', lines[0])
            # --out still receives every tick
            self.assertEqual(len(out.read_text(encoding="utf-8").splitlines()), 2)

    def test_quiet_helper_flag_wins(self) -> None:
        self.assertTrue(watch.quiet("JEV_X_WATCH_QUIET", True))
        self.assertTrue(watch.quiet("JEV_X_WATCH_QUIET", flag=True))
        self.assertFalse(watch.quiet("JEV_X_WATCH_QUIET", False))

    def test_quiet_helper_reads_env(self) -> None:
        for val, want in (
            ("1", True),
            ("true", True),
            ("yes", True),
            ("on", True),
            ("0", False),
            ("no", False),
            ("", False),
            ("garbage", False),
        ):
            with patch.dict(os.environ, {"JEV_X_WATCH_QUIET": val}):
                self.assertIs(
                    watch.quiet("JEV_X_WATCH_QUIET", False), want, val
                )

    def test_quiet_env_suppresses_clean_ticks_via_emit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "t.jsonl"
            buf = io.StringIO()
            with patch.dict(os.environ, {"JEV_X_WATCH_QUIET": "1"}):
                with redirect_stdout(buf):
                    watch.emit(
                        {"ok": True}, out,
                        quiet=watch.quiet("JEV_X_WATCH_QUIET", False), bad=False,
                    )
                    watch.emit(
                        {"ok": False}, out,
                        quiet=watch.quiet("JEV_X_WATCH_QUIET", False), bad=True,
                    )
            self.assertEqual(len(buf.getvalue().splitlines()), 1)
            self.assertEqual(len(out.read_text(encoding="utf-8").splitlines()), 2)

    def test_quiet_off_prints_everything(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            watch.emit({"ok": True}, None, quiet=False, bad=False)
        self.assertEqual(len(buf.getvalue().splitlines()), 1)

    def test_no_out_path_skips_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with redirect_stdout(buf):
                watch.emit({"a": 1}, "")
                watch.emit({"a": 2}, None)
            self.assertEqual(len(list(Path(tmp).iterdir())), 0)
            self.assertEqual(len(buf.getvalue().splitlines()), 2)


class WriteVerdictTests(unittest.TestCase):
    def test_writes_payload_and_leaves_no_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v.json"
            self.assertTrue(watch.write_verdict(str(path), {"verdict": "ok", "n": 1}))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["verdict"], "ok")
            self.assertFalse((Path(tmp) / "v.json.tmp").exists())
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["v.json"])

    def test_atomic_overwrite_replaces_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v.json"
            watch.write_verdict(str(path), {"verdict": "one"})
            watch.write_verdict(str(path), {"verdict": "two"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["verdict"], "two")

    def test_bad_path_returns_false_and_warns(self) -> None:
        buf_err = io.StringIO()
        missing_dir = Path("nul\\bad") / "nope"
        with patch.object(sys, "stderr", buf_err):
            ok = watch.write_verdict(str(missing_dir / "v.json"), {"a": 1})
        self.assertFalse(ok)
        self.assertIn("--verdict failed", buf_err.getvalue())

    def test_injects_ts_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v.json"
            watch.write_verdict(str(path), {"verdict": "ok"})
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("ts", payload)
            self.assertIsInstance(payload["ts"], int)
            self.assertGreater(payload["ts"], 0)

    def test_preserves_caller_ts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v.json"
            watch.write_verdict(str(path), {"verdict": "ok", "ts": 1234})
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["ts"], 1234)


class WriteVerdictAtomicityTests(unittest.TestCase):
    def test_replace_failure_cleans_up_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v.json"
            buf_err = io.StringIO()
            with patch.object(
                watch.os, "replace", side_effect=OSError("boom")
            ), patch.object(sys, "stderr", buf_err):
                ok = watch.write_verdict(str(path), {"verdict": "x"})
            self.assertFalse(ok)
            self.assertIn("--verdict failed", buf_err.getvalue())
            self.assertFalse(path.exists())
            self.assertFalse((Path(tmp) / "v.json.tmp").exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_write_failure_leaves_no_tmp(self) -> None:
        # target dir is a file: tmp write fails before any replace
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocker"
            blocker.write_text("x", encoding="utf-8")
            buf_err = io.StringIO()
            with patch.object(sys, "stderr", buf_err):
                ok = watch.write_verdict(str(blocker / "v.json"), {"a": 1})
            self.assertFalse(ok)
            self.assertIn("--verdict failed", buf_err.getvalue())
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["blocker"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

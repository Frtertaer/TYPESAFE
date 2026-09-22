#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for scripts/_watch.py shared watch-tick plumbing."""
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

    def test_override_wins_over_env(self) -> None:
        with patch.dict("os.environ", {"JEV_X_WATCH_MAX": "9"}):
            self.assertEqual(watch.cap("JEV_X_WATCH_MAX", 2), 2)
            self.assertEqual(watch.cap("JEV_X_WATCH_MAX", "3"), 3)
            self.assertEqual(watch.cap("JEV_X_WATCH_MAX", -4), 0)
            self.assertEqual(watch.cap("JEV_X_WATCH_MAX", "junk"), 0)
            self.assertEqual(watch.cap("JEV_X_WATCH_MAX", 0), 9)


class DeadlineTests(unittest.TestCase):
    def test_deadline_zero_means_unbounded(self) -> None:
        self.assertEqual(watch.deadline(0), 0.0)
        self.assertEqual(watch.deadline(None), 0.0)
        self.assertEqual(watch.deadline("junk"), 0.0)
        self.assertEqual(watch.deadline(-2), 0.0)

    def test_deadline_is_now_plus_seconds(self) -> None:
        import time

        before = time.time()
        d = watch.deadline(10)
        self.assertTrue(before + 9.9 < d < before + 10.1)
        self.assertTrue(watch.deadline("5") > time.time())


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
            self.assertEqual(json.loads(lines[0]), {"a": 1})
            self.assertEqual(json.loads(lines[1]), {"a": 2})

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


if __name__ == "__main__":
    unittest.main(verbosity=2)

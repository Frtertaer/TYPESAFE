#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _watch  # noqa: E402


class DigTests(unittest.TestCase):
    PAYLOAD = {
        "verdict": "pass",
        "findings": [{"rule": "J001", "severity": "warn"}],
        "nested": {"deep": {"leaf": 42}},
    }

    def test_dig_dict_key_hit(self) -> None:
        value, ok = _watch.dig(self.PAYLOAD, "verdict")
        self.assertTrue(ok)
        self.assertEqual(value, "pass")

    def test_dig_nested_path(self) -> None:
        value, ok = _watch.dig(self.PAYLOAD, "nested.deep.leaf")
        self.assertTrue(ok)
        self.assertEqual(value, 42)

    def test_dig_list_index(self) -> None:
        value, ok = _watch.dig(self.PAYLOAD, "findings.0.rule")
        self.assertTrue(ok)
        self.assertEqual(value, "J001")

    def test_dig_missing_key(self) -> None:
        value, ok = _watch.dig(self.PAYLOAD, "nope")
        self.assertFalse(ok)
        self.assertIsNone(value)

    def test_dig_index_out_of_range(self) -> None:
        value, ok = _watch.dig(self.PAYLOAD, "findings.9")
        self.assertFalse(ok)
        self.assertIsNone(value)

    def test_dig_non_numeric_part_on_list(self) -> None:
        value, ok = _watch.dig(self.PAYLOAD, "findings.rule")
        self.assertFalse(ok)
        self.assertIsNone(value)

    def test_dig_into_scalar_fails(self) -> None:
        value, ok = _watch.dig(self.PAYLOAD, "verdict.deeper")
        self.assertFalse(ok)
        self.assertIsNone(value)

    def test_dig_falsey_value_still_hit(self) -> None:
        value, ok = _watch.dig({"a": {"b": 0}}, "a.b")
        self.assertTrue(ok)
        self.assertEqual(value, 0)


class SameTickTests(unittest.TestCase):
    def test_identical_modulo_volatile(self) -> None:
        prev = {"ts": 1.0, "verdict": "pass", "failures": 0}
        tick = {"ts": 2.0, "elapsed_s": 5, "verdict": "pass", "failures": 0}
        self.assertTrue(_watch.same_tick(prev, tick))

    def test_differing_field_returns_false(self) -> None:
        prev = {"ts": 1.0, "verdict": "pass"}
        tick = {"ts": 2.0, "verdict": "fail"}
        self.assertFalse(_watch.same_tick(prev, tick))

    def test_none_prev_returns_false(self) -> None:
        self.assertFalse(_watch.same_tick(None, {"a": 1}))
        self.assertFalse(_watch.same_tick({"a": 1}, None))
        self.assertFalse(_watch.same_tick("x", {"a": 1}))

    def test_custom_ignore(self) -> None:
        prev = {"ts": 1, "run": 1, "v": "x"}
        tick = {"ts": 2, "run": 2, "v": "x"}
        self.assertFalse(_watch.same_tick(prev, tick))
        self.assertTrue(_watch.same_tick(prev, tick, ignore=("ts", "run")))

    def test_key_added_between_ticks(self) -> None:
        prev = {"ts": 1, "v": "x"}
        tick = {"ts": 2, "v": "x", "extra": 1}
        self.assertFalse(_watch.same_tick(prev, tick))


class CapTests(unittest.TestCase):
    def test_override_wins_over_env(self) -> None:
        import os

        old = os.environ.get("JEV_TEST_CAP")
        os.environ["JEV_TEST_CAP"] = "7"
        try:
            self.assertEqual(_watch.cap("JEV_TEST_CAP", override="3"), 3)
            self.assertEqual(_watch.cap("JEV_TEST_CAP"), 7)
        finally:
            if old is None:
                os.environ.pop("JEV_TEST_CAP", None)
            else:
                os.environ["JEV_TEST_CAP"] = old

    def test_zero_and_bad_override_uncapped(self) -> None:
        self.assertEqual(_watch.cap("JEV_TEST_CAP", override="0"), 0)
        self.assertEqual(_watch.cap("JEV_TEST_CAP", override="junk"), 0)

    def test_bad_env_warns_and_uncapped(self) -> None:
        import io
        import os
        from contextlib import redirect_stderr

        old = os.environ.get("JEV_TEST_CAP")
        os.environ["JEV_TEST_CAP"] = "bogus"
        err = io.StringIO()
        try:
            with redirect_stderr(err):
                n = _watch.cap("JEV_TEST_CAP")
        finally:
            if old is None:
                os.environ.pop("JEV_TEST_CAP", None)
            else:
                os.environ["JEV_TEST_CAP"] = old
        self.assertEqual(n, 0)
        self.assertIn("bad JEV_TEST_CAP", err.getvalue())


class EmitOrJqTests(unittest.TestCase):
    def test_jq_prints_field_lines_not_tick(self) -> None:
        import io
        from contextlib import redirect_stdout

        tick = {"ts": 1, "a": {"b": 5}, "c": [1, 2]}
        buf = io.StringIO()
        with redirect_stdout(buf):
            _watch.emit_or_jq(tick, "a.b,c")
        lines = buf.getvalue().splitlines()
        self.assertEqual(json.loads(lines[0]), 5)
        self.assertEqual(json.loads(lines[1]), [1, 2])
        self.assertNotIn('"ts"', buf.getvalue())

    def test_jq_miss_prints_null(self) -> None:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            _watch.emit_or_jq({"a": 1}, "nope.x")
        self.assertEqual(buf.getvalue().strip(), "null")

    def test_no_jq_emits_tick_json(self) -> None:
        import io
        from contextlib import redirect_stdout

        tick = {"ts": 1, "verdict": "pass"}
        buf = io.StringIO()
        with redirect_stdout(buf):
            _watch.emit_or_jq(tick, "")
        self.assertEqual(json.loads(buf.getvalue()), tick)

    def test_quiet_suppresses_jq_projection_on_clean_tick(self) -> None:
        """--quiet must silence --jq projections on clean ticks, same as it
        silences whole-tick output."""
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            _watch.emit_or_jq({"a": 1}, "a", quiet=True, bad=False)
        self.assertEqual(buf.getvalue(), "")

    def test_quiet_keeps_jq_projection_on_bad_tick(self) -> None:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            _watch.emit_or_jq({"a": 1}, "a", quiet=True, bad=True)
        self.assertEqual(buf.getvalue().strip(), "1")


if __name__ == "__main__":
    unittest.main()

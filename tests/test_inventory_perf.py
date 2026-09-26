#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_inventory():
    path = ROOT / "skills" / "jev-consult" / "scripts" / "inventory.py"
    spec = importlib.util.spec_from_file_location("jev_inventory_perf", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


inv = load_inventory()


def make_items(n: int) -> list[dict]:
    return [
        {
            "id": "skill:%d" % i,
            "name": "tool-%d" % i,
            "description": "generic tool %d for task %d" % (i, i % 97),
        }
        for i in range(n)
    ]


class ShortlistPerfTests(unittest.TestCase):
    def test_shortlist_handles_large_item_list(self) -> None:
        items = make_items(4000)
        # a rare query token lowers min_keep so matching names rank in
        items[1]["name"] = "needlefell tool"
        start = time.monotonic()
        picked = inv.shortlist(items, "needlefell refactor search", 20, [])
        elapsed = time.monotonic() - start
        # linear in items; generous bound so slow shared CI runners still pass
        self.assertLess(elapsed, 30.0)
        self.assertLessEqual(len(picked), 20)
        # the rare-name item should rank in the shortlist
        self.assertIn("skill:1", {item["id"] for item in picked})

    def test_shortlist_empty_query_returns_nothing(self) -> None:
        picked = inv.shortlist(make_items(50), "", 10, [])
        self.assertEqual(picked, [])

    def test_name_df_counts_tokens_across_items(self) -> None:
        items = [
            {"id": "a", "name": "alpha beta", "description": ""},
            {"id": "b", "name": "alpha gamma", "description": ""},
            {"id": "c", "name": "delta", "description": "beta hidden"},
        ]
        df = inv.name_df(items, {"alpha", "beta"})
        # name_df counts NAME token occurrences only
        self.assertEqual(df["alpha"], 2)
        self.assertEqual(df["beta"], 1)

    def test_shortlist_extra_pins_named_items(self) -> None:
        items = make_items(30)
        picked = inv.shortlist(items, "unrelated-query", 5, ["tool-29"])
        self.assertEqual(picked[0]["id"], "skill:29")


if __name__ == "__main__":
    unittest.main()

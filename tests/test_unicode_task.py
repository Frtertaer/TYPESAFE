#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Non-ASCII task text must still drive the shortlist: tokens() used to
match [a-z0-9] only, so a fully Cyrillic or CJK task produced an empty
query and every item scored 0."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


INV = load(ROOT / "skills" / "jev-consult" / "scripts" / "inventory.py", "jev_inventory")


class UnicodeTaskTests(unittest.TestCase):
    def test_cyrillic_task_yields_tokens(self) -> None:
        got = INV.tokens("настройка прокси для сети")
        self.assertIn("настройка", got)
        self.assertIn("прокси", got)
        self.assertNotIn("the", got)  # stop-word still filtered

    def test_accented_task_yields_tokens(self) -> None:
        got = INV.tokens("résumé naïve café")
        self.assertIn("résumé", got)
        self.assertIn("café", got)

    def test_ascii_behavior_unchanged(self) -> None:
        self.assertEqual(
            INV.tokens("deploy the web_app to prod"),
            {"deploy", "web", "app", "prod"},
        )

    def test_cyrillic_task_shortlists_matching_skill(self) -> None:
        items = [
            {"id": "s1", "kind": "skill", "name": "настройка сети",
             "description": "настройка прокси и сети"},
            {"id": "s2", "kind": "skill", "name": "docker deploy",
             "description": "container deployment pipeline"},
        ]
        picked = INV.shortlist(items, "настройка прокси", 5, [])
        self.assertTrue(picked)
        self.assertEqual(picked[0]["id"], "s1")

    def test_unmatched_query_still_empty_score(self) -> None:
        item = {"id": "s1", "kind": "skill", "name": "x",
                "description": "y"}
        self.assertEqual(
            INV.score_item(item, {"zzzzzz"}), 0)


if __name__ == "__main__":
    unittest.main()

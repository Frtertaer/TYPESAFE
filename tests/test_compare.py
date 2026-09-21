#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "jev-consult" / "scripts" / "compare.py"


def load_compare():
    spec = importlib.util.spec_from_file_location("jev_compare", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


cmp_mod = load_compare()


def load_jev():
    path = ROOT / "skills" / "jev-consult" / "scripts" / "jev.py"
    spec = importlib.util.spec_from_file_location("jev_consult_jev", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class CompareTests(unittest.TestCase):
    def test_cases_have_before_without_jev_and_after_with_jev(self) -> None:
        blob = cmp_mod.load_cases()
        ids = [case["id"] for case in blob["cases"]]
        self.assertEqual(ids, ["off_track", "unknown", "stuck", "forget"])
        for case in blob["cases"]:
            self.assertFalse(case["before"]["called_jev"])
            self.assertTrue(case["after"]["called_jev"])
            self.assertIn(case["score"], ("on_track", "grounded_enough"))

    def test_offline_table_mentions_defects(self) -> None:
        result = cmp_mod.run(live=False, as_json=True)
        text = cmp_mod.format_table(result["rows"], live=False)
        self.assertIn("off_track", text)
        self.assertIn("forget", text)
        self.assertIn("return_to_plan", text)
        self.assertIn("No watchdog", text)

    def test_build_request_uses_policy_template(self) -> None:
        jev = load_jev()
        policy = jev.load_policy(str(ROOT / "skills" / "jev-consult" / "policy.json"))
        case = cmp_mod.load_cases()["cases"][0]
        request = cmp_mod.build_request(policy, case, case["before"])
        self.assertEqual(request["questions"]["on_track"]["type"], "noul")
        self.assertIn("install.py", request["state"]["current_step"])
        jev.validate_questions(request["questions"], policy)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(verbosity=2) else 1)

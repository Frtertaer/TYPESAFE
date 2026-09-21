#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "jev-consult"
VENDOR = ROOT / "vendor" / "awesome-llm-apps-skill-evals"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SCANNER = load(SKILL / "scripts" / "skill_scanner.py", "jev_skill_scanner")
TRIGGER = load(VENDOR / "run_trigger_evals.py", "jev_run_trigger_evals")


class SkillEvalsTests(unittest.TestCase):
    def test_skill_lint_strict_passes(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(VENDOR / "skill_lint.py"), str(SKILL), "--strict"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_scanner_no_critical_or_undeclared_net(self) -> None:
        findings = SCANNER.scan_skill(str(SKILL))
        critical = [f for f in findings if f.severity == "CRITICAL"]
        self.assertEqual(critical, [])
        net_undeclared = [
            f for f in findings if f.check == "NET01" and f.severity != "INFO"
        ]
        self.assertEqual(net_undeclared, [])

    def test_trigger_eval_margin(self) -> None:
        cases_path = ROOT / "tests" / "fixtures" / "jev-consult.trigger-cases.json"
        cases = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]
        desc_toks = TRIGGER.tokens(TRIGGER.description_of(str(SKILL)))
        pos: list[tuple[float, str]] = []
        neg: list[tuple[float, str]] = []
        for case in cases:
            s = TRIGGER.score(TRIGGER.tokens(case["prompt"]), desc_toks)
            (pos if case["should_trigger"] else neg).append((s, case["id"]))
        for s, cid in pos:
            self.assertGreater(s, 0, "positive %r scored 0" % cid)
        worst_pos = min(s for s, _ in pos)
        best_neg = max(s for s, _ in neg)
        self.assertGreater(
            worst_pos,
            best_neg * TRIGGER.MARGIN,
            "weakest positive %.3f does not clear strongest negative %.3f "
            "(margin %.2f); scores: %s"
            % (worst_pos, best_neg, TRIGGER.MARGIN, pos + neg),
        )


if __name__ == "__main__":
    unittest.main()

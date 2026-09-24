#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Hook scan+decide latency on a large synthetic harness tree.

400 skills/plugins must scan, IDF-shortlist and emit well under the
hook budget (policy hook_budget_seconds). Jev is stubbed out —
this pins the local work only."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
INV = load(ROOT / "skills" / "jev-consult" / "scripts" / "inventory.py", "jev_inventory")
HOOK = load(ROOT / "skills" / "jev-consult" / "scripts" / "inventory_hook.py", "jev_inventory_hook")

N_ITEMS = 400


def skip_pick(*_args, **_kwargs):
    return {"status": "skip", "winner": None}


class HookLatencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._log_env = patch.dict(os.environ, {"JEV_CONSULT_LOG": "0"})
        self._log_env.start()
        self.addCleanup(self._log_env.stop)

    def _big_fixture(self, root: Path) -> None:
        root.mkdir(parents=True)
        (root / "config.yaml").write_text("x: 1\n", encoding="utf-8")
        skills = root / "skills"
        plugins = root / "plugins"
        for i in range(N_ITEMS):
            d = skills / ("skill-%03d" % i)
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(
                "---\nname: skill-%03d\ndescription: handles keyword-%d "
                "task family with jwt, oauth, deploy, lint terms\n---\n" % (i, i % 17),
                encoding="utf-8",
            )
            p = plugins / ("plug-%03d" % i)
            p.mkdir(parents=True)
            (p / "plugin.yaml").write_text(
                "name: plug-%03d\ndescription: plugin %d with auth and "
                "cache responsibilities\n" % (i, i % 19),
                encoding="utf-8",
            )

    def test_scan_and_handle_under_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "harness"
            self._big_fixture(root)
            t0 = time.monotonic()
            items = INV.scan("hermes", hermes=root)
            self.assertGreaterEqual(len(items), N_ITEMS)
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "wire jwt auth into the api",
                    "cwd": str(Path(tmp) / "work"),
                },
                items=items,
                harness="claude-code",
                pick_fn=skip_pick,
            )
            elapsed = time.monotonic() - t0
            self.assertIsInstance(out, dict)
            budget = HOOK.hook_budget_seconds() if hasattr(HOOK, "hook_budget_seconds") else 12.0
            self.assertLess(
                elapsed, budget,
                "scan+handle took %.1fs over hook budget %.1fs" % (elapsed, budget),
            )


if __name__ == "__main__":
    unittest.main()

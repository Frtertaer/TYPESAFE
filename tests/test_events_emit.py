#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""`inventory_hook --events` emit-shape pin:
text = one name per line (sorted), --json = array, --jsonl =
{"event": ...} rows, --csv/--md = `event` column — all honoring
JEV_HOOK_EVENTS/JEV_HOOK_SKIP_EVENTS."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "jev-consult" / "scripts" / "inventory_hook.py"

DEFAULT_EVENTS = ["UserPromptSubmit", "pre_llm_call"]


def run(argv: list, env: dict | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e["JEV_CONSULT_LOG"] = "0"
    e.pop("JEV_HOOK_EVENTS", None)
    e.pop("JEV_HOOK_SKIP_EVENTS", None)
    if env:
        e.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
        env=e,
    )


class EventsEmitTests(unittest.TestCase):
    def test_text_one_per_line_sorted(self) -> None:
        proc = run(["--events"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.splitlines(), DEFAULT_EVENTS)

    def test_json_array(self) -> None:
        proc = run(["--events", "--json"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout), DEFAULT_EVENTS)

    def test_jsonl_rows_have_event_key(self) -> None:
        proc = run(["--events", "--jsonl"])
        self.assertEqual(proc.returncode, 0)
        rows = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
        self.assertEqual([r["event"] for r in rows], DEFAULT_EVENTS)

    def test_csv_md_event_column(self) -> None:
        proc = run(["--events", "--csv"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.splitlines()[0], "event")
        self.assertIn("UserPromptSubmit", proc.stdout)
        proc = run(["--events", "--md"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.splitlines()[0].strip(), "| event |")

    def test_env_override_and_skip(self) -> None:
        proc = run(
            ["--events", "--json"],
            env={"JEV_HOOK_EVENTS": "A,B,C", "JEV_HOOK_SKIP_EVENTS": "B"},
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout), ["A", "C"])

    def test_skip_all_emits_empty(self) -> None:
        env = {"JEV_HOOK_SKIP_EVENTS": ",".join(DEFAULT_EVENTS)}
        proc = run(["--events", "--json"], env=env)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout), [])


if __name__ == "__main__":
    unittest.main()

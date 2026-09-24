#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""--verdict PATH contract:
- writes one JSON object to PATH (atomically: no lingering .tmp sibling)
- injects a ``ts`` epoch int when the caller didn't set one
- `--verdict -` streams the payload: stdout for CLIs, stderr for hooks
  (hook stdout stays pure — the emitted payload only)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"


def run(script: str, argv: list, stdin: str = "", env: dict | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e["JEV_CONSULT_LOG"] = "0"
    if env:
        e.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *argv],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(ROOT),
        env=e,
    )


def assert_verdict_file(test: unittest.TestCase, path: Path) -> dict:
    test.assertTrue(path.is_file(), "verdict file missing")
    payload = json.loads(path.read_text(encoding="utf-8"))
    test.assertIsInstance(payload, dict)
    test.assertIsInstance(payload.get("ts"), int, "verdict must inject ts epoch")
    test.assertIsInstance(payload.get("verdict"), str)
    tmp = path.with_name(path.name + ".tmp")
    test.assertFalse(tmp.exists(), "lingering .tmp sibling")
    return payload


class VerdictShapeTests(unittest.TestCase):
    def test_policy_lint_verdict_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            v = Path(tmp) / "v.json"
            proc = run("policy_lint.py", ["--verdict", str(v)])
            self.assertIn(proc.returncode, (0, 1))
            payload = assert_verdict_file(self, v)
            self.assertIn(payload["verdict"], ("pass", "fail"))

    def test_compact_hook_verdict_file_and_dash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            v = Path(tmp) / "v.json"
            proc = run(
                "compact_hook.py",
                ["--simulate", "tiny", "--verdict", str(v)],
            )
            self.assertEqual(proc.returncode, 0)
            payload = assert_verdict_file(self, v)
            self.assertEqual(payload["verdict"], "skip")
            # `-` variant: payload on stderr, stdout stays pure payload.
            proc = run("compact_hook.py", ["--simulate", "tiny", "--verdict", "-"])
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(json.loads(proc.stdout.strip()), {})
            verdict = json.loads(proc.stderr.strip())
            self.assertEqual(verdict["verdict"], "skip")

    def test_decisions_verdict_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            log.write_text(
                json.dumps(
                    {"ts": 1.0, "harness": "x", "jev_status": "none",
                     "outcome": "miss", "fill": "none", "winner": None,
                     "need": 0.1}
                )
                + "\n",
                encoding="utf-8",
            )
            v = Path(tmp) / "v.json"
            proc = run(
                "decisions.py",
                ["--verdict", str(v)],
                env={"JEV_DECISIONS": str(log)},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[:300])
            payload = assert_verdict_file(self, v)
            self.assertEqual(payload["verdict"], "ok")

    def test_inventory_hook_verdict_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            v = Path(tmp) / "v.json"
            proc = run(
                "inventory_hook.py",
                ["--file", "-", "--verdict", str(v), "--watch", "1",
                 "--max-ticks", "1"],
                stdin="{}",
                env={"JEV_HOOK_CWD": tmp, "JEV_HOOK_HARNESS": "claude-code"},
            )
            self.assertIn(proc.returncode, (0, 1), proc.stderr[:400])
            if v.is_file():
                payload = assert_verdict_file(self, v)
                self.assertIn("ticks", payload)


if __name__ == "__main__":
    unittest.main()

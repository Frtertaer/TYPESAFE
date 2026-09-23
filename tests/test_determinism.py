"""Stable JSON output: repeated runs on identical input must be byte-identical."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
HARNESS = ROOT / "tests" / "fixtures" / "inventory-harness"


def run(script: str, argv: list, env: dict | None = None, cwd: str | None = None) -> bytes:
    e = dict(os.environ)
    e.pop("JEV_CONSULT_LOG", None)
    e["JEV_CONSULT_LOG"] = "0"
    if env:
        e.update(env)
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *argv],
        capture_output=True,
        cwd=cwd or str(ROOT),
        env=e,
    )
    return out.stdout


def write_log(path: Path) -> None:
    entries = [
        {"ts": 1700000000.0, "harness": "hermes", "jev_status": "ok",
         "outcome": "winner", "fill": "idf", "winner": "a", "need": 0.5},
        {"ts": 1700000100.0, "harness": "claude-code", "jev_status": "none",
         "outcome": "miss", "fill": "none", "winner": None, "need": 0.2},
        {"ts": 1700000200.0, "harness": "hermes", "jev_status": "error",
         "outcome": "error", "fill": "idf", "winner": None, "need": 0.0},
    ]
    path.write_text(
        "\n".join(json.dumps(e, sort_keys=True) for e in entries) + "\n",
        encoding="utf-8",
    )


class DeterminismTests(unittest.TestCase):
    def test_decisions_json_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            write_log(log)
            env = {"JEV_DECISIONS": str(log)}
            a = run("decisions.py", ["--json"], env=env)
            b = run("decisions.py", ["--json"], env=env)
        self.assertEqual(a, b)
        self.assertTrue(a.strip())

    def test_decisions_csv_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            write_log(log)
            env = {"JEV_DECISIONS": str(log)}
            a = run("decisions.py", ["--csv"], env=env)
            b = run("decisions.py", ["--csv"], env=env)
        self.assertEqual(a, b)
        self.assertTrue(a.strip())

    def test_inventory_json_byte_identical(self) -> None:
        a = run("inventory.py", ["--task", "jwt"], cwd=str(HARNESS))
        b = run("inventory.py", ["--task", "jwt"], cwd=str(HARNESS))
        self.assertEqual(a, b)
        self.assertTrue(a.strip())

    def test_hook_env_report_byte_identical(self) -> None:
        a = run("inventory_hook.py", ["--env"], cwd=str(HARNESS))
        b = run("inventory_hook.py", ["--env"], cwd=str(HARNESS))
        self.assertEqual(a, b)
        self.assertTrue(a.strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)

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

    def test_schema_outputs_byte_identical(self) -> None:
        """--schema (or trace `schema`) must print byte-identical output
        on repeat runs — the contract is pure metadata."""
        flag = (
            "apply_fill.py", "catalog_fill.py", "compact.py",
            "compare.py", "decisions.py", "doctor.py", "inventory.py",
            "inventory_hook.py", "jev.py", "peer_fill.py",
            "policy_lint.py", "progress.py", "question_lint.py",
            "skill_lint.py", "trigger_eval.py", "trigger_lint.py",
        )
        for name in sorted(flag):
            with self.subTest(script=name):
                a = run(name, ["--schema"])
                b = run(name, ["--schema"])
                self.assertEqual(a, b, name)
                self.assertTrue(a.strip(), name)
        with self.subTest(script="trace.py schema"):
            a = run("trace.py", ["schema"])
            b = run("trace.py", ["schema"])
            self.assertEqual(a, b)
            self.assertTrue(a.strip())

    def test_env_reports_byte_identical(self) -> None:
        """--env reports are pure config resolution — repeat runs must
        be byte-identical."""
        for name in ("compact.py", "compare.py", "doctor.py",
                     "decisions.py", "inventory.py"):
            with self.subTest(script=name):
                a = run(name, ["--env"], cwd=str(HARNESS))
                b = run(name, ["--env"], cwd=str(HARNESS))
                self.assertEqual(a, b, name)
                self.assertTrue(a.strip(), name)

    def test_trace_listings_byte_identical(self) -> None:
        """notes/history on a fixed trace file repeat byte-identically."""
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / ".jev-trace.json"
            trace.write_text(
                json.dumps(
                    {
                        "plan": "p",
                        "current_step": "s",
                        "attempt_count": 1,
                        "last_error": "",
                        "unknown": "",
                        "inspected": [],
                        "last_pick": "a",
                        "history": [
                            {"ts": 1000, "iso": "2026-01-01T00:00:00Z",
                             "kind": "approach", "pick": "a"},
                            {"ts": 2000, "iso": "2026-01-01T00:01:00Z",
                             "pick": "b"},
                        ],
                        "notes": [
                            {"ts": 1000, "iso": "2026-01-01T00:00:00Z",
                             "text": "n1", "sha": "s1"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            for sub in (["notes", "--json"], ["history", "--json"],
                        ["notes"], ["history"]):
                with self.subTest(args=sub):
                    a = run("trace.py",
                            ["--file", str(trace)] + sub)
                    b = run("trace.py",
                            ["--file", str(trace)] + sub)
                    self.assertEqual(a, b)
                    self.assertTrue(a.strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)

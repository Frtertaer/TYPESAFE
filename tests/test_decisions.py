import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import decisions


def write_log(path: Path, entries: list) -> None:
    lines = [json.dumps(e) if isinstance(e, dict) else e for e in entries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class LoadEntriesTest(unittest.TestCase):
    def test_skips_bad_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(path, [{"jev_status": "winner"}, "not-json", "", '{"x": 1}', "[1]"])
            entries, bad = decisions.load_entries(path)
        self.assertEqual(len(entries), 2)
        self.assertEqual(bad, 2)

    def test_missing_file_returns_empty(self):
        entries, bad = decisions.load_entries(Path("does-not-exist.jsonl"))
        self.assertEqual(entries, [])
        self.assertEqual(bad, 0)


class SummarizeTest(unittest.TestCase):
    def test_counts(self):
        entries = [
            {
                "harness": "claude-code",
                "jev_status": "winner",
                "explicit": True,
                "strong_pick": True,
                "winner": {"kind": "skill", "name": "alpha"},
                "need": 0.2,
                "latency_ms": 100,
            },
            {
                "harness": "hermes",
                "jev_status": "none",
                "need": 0.8,
                "latency_ms": 300,
            },
            {
                "harness": "hermes",
                "jev_status": "winner",
                "winner": {"kind": "skill", "name": "alpha"},
                "need": 0.4,
                "latency_ms": 200,
            },
        ]
        stats = decisions.summarize(entries, bad=1)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["bad_lines"], 1)
        self.assertEqual(stats["by_status"]["winner"], 2)
        self.assertEqual(stats["by_status"]["none"], 1)
        self.assertEqual(stats["by_harness"]["hermes"], 2)
        self.assertEqual(stats["explicit"], 1)
        self.assertEqual(stats["strong_pick"], 1)
        self.assertEqual(stats["need_skill"]["n"], 3)
        self.assertAlmostEqual(stats["need_skill"]["mean"], 0.4667, places=3)
        self.assertEqual(stats["latency_ms"]["p50"], 200)
        self.assertEqual(stats["latency_ms"]["max"], 300)
        self.assertEqual(stats["top_winners"]["skill:alpha"], 2)

    def test_empty(self):
        stats = decisions.summarize([])
        self.assertEqual(stats["total"], 0)
        self.assertIsNone(stats["need_skill"]["mean"])
        self.assertIsNone(stats["latency_ms"]["p50"])


class CliTest(unittest.TestCase):
    def run_cli(self, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "decisions.py"), *argv],
            capture_output=True,
            text=True,
            env=full_env,
        )

    def test_stats_and_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {
                        "ts": 1700000000,
                        "harness": "codex",
                        "jev_status": "winner",
                        "winner": {"kind": "skill", "name": "beta"},
                        "prompt_head": "fix the flaky test",
                        "need": 0.1,
                        "latency_ms": 50,
                    }
                ],
            )
            proc = self.run_cli("--file", str(path), "--tail", "5")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("entries: 1", proc.stdout)
            self.assertIn("winner=1", proc.stdout)
            self.assertIn("beta", proc.stdout)
            self.assertIn("fix the flaky test", proc.stdout)

    def test_json_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(path, [{"jev_status": "idf"}])
            proc = self.run_cli("--file", str(path), "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["by_status"]["idf"], 1)

    def test_missing_log_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self.run_cli("--file", str(Path(tmp) / "none.jsonl"))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("no decisions log", proc.stderr)

    def test_disabled_log_fails_cleanly(self):
        proc = self.run_cli(env={"JEV_CONSULT_LOG": "0"})
        self.assertEqual(proc.returncode, 2)
        self.assertIn("disabled", proc.stderr)


if __name__ == "__main__":
    unittest.main()

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

    def test_malformed_fields_tolerated(self):
        entries = [
            {"jev_status": "winner", "winner": "just-a-string", "need": "high", "latency_ms": "fast"},
            {"jev_status": "winner", "winner": {"name": ""}, "need": True, "latency_ms": None},
            {},
        ]
        stats = decisions.summarize(entries)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["top_winners"], {})
        self.assertEqual(stats["need_skill"]["n"], 1)  # bool counts as numeric
        self.assertEqual(stats["by_status"]["unknown"], 1)

    def test_percentile_bounds(self):
        self.assertIsNone(decisions._percentile([], 0.5))
        self.assertEqual(decisions._percentile([5.0], 0.9), 5.0)
        self.assertEqual(decisions._percentile([1.0, 2.0, 3.0], 0.0), 1.0)
        self.assertEqual(decisions._percentile([1.0, 2.0, 3.0], 1.0), 3.0)

    def test_time_str_bad_values(self):
        self.assertEqual(decisions.time_str(None), "?")
        self.assertEqual(decisions.time_str("nope"), "?")
        self.assertRegex(decisions.time_str(1700000000), r"\d{2}-\d{2} \d{2}:\d{2}")

    def test_format_entry_non_dict_winner(self):
        line = decisions.format_entry(
            {"ts": 1700000000, "harness": "hermes", "jev_status": "winner", "winner": "x", "prompt_head": "hi"}
        )
        self.assertIn("hermes", line)
        self.assertIn("-", line)
        self.assertIn("hi", line)


def run_cli(*argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "decisions.py"), *argv],
        capture_output=True,
        text=True,
        env=full_env,
    )


class CliTest(unittest.TestCase):
    run_cli = staticmethod(run_cli)

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


class FilterSinceTest(unittest.TestCase):
    run_cli = staticmethod(run_cli)

    def test_filter_since(self):
        entries = [
            {"ts": 1000.0, "jev_status": "idf"},
            {"ts": 2000.0, "jev_status": "winner"},
            {"jev_status": "no-ts"},
            {"ts": "bad", "jev_status": "bad-ts"},
        ]
        out = decisions.filter_since(entries, 1500.0)
        self.assertEqual([e["jev_status"] for e in out], ["winner"])
        self.assertIs(decisions.filter_since(entries, None), entries)

    def test_main_since_and_days(self):
        import time as _time
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            now = _time.time()
            write_log(
                path,
                [
                    {"ts": now - 10 * 86400, "jev_status": "idf"},
                    {"ts": now - 100, "jev_status": "winner"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--json", "--since", str(now - 1000))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["total"], 1)
            self.assertEqual(stats["filtered"], 1)
            proc = self.run_cli("--file", str(path), "--json", "--days", "1")
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["total"], 1)
            self.assertEqual(stats["by_status"]["winner"], 1)
            proc = self.run_cli("--file", str(path), "--json")
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["total"], 2)
            self.assertIsNone(stats["since"])


if __name__ == "__main__":
    unittest.main()

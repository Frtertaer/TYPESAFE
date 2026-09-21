import csv
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

    def test_top_prompts_counted(self):
        entries = [
            {"prompt_head": "fix the flaky test", "jev_status": "winner"},
            {"prompt_head": "fix the flaky test", "jev_status": "none"},
            {"prompt_head": "other task", "jev_status": "winner"},
            {"prompt_head": "", "jev_status": "idf"},
            {"jev_status": "idf"},
        ]
        stats = decisions.summarize(entries)
        self.assertEqual(stats["top_prompts"], {"fix the flaky test": 2, "other task": 1})
        text = decisions.format_stats(stats)
        self.assertIn("top prompts:", text)
        self.assertIn("fix the flaky test", text)
        self.assertEqual(decisions.summarize([])["top_prompts"], {})

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

    def test_csv_output(self):
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
                        "prompt_head": "fix the test",
                    },
                    {"ts": 1700000001, "harness": "grok", "jev_status": "idf"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--csv")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            rows = list(csv.reader(proc.stdout.splitlines()))
            self.assertEqual(
                rows[0],
                ["ts", "harness", "jev_status", "winner", "dedupe", "fill", "outcome", "prompt_head"],
            )
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[1][1], "codex")
            self.assertEqual(rows[1][3], "beta")
            self.assertEqual(rows[2][1], "grok")

    def test_csv_includes_fill_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [{"harness": "codex", "jev_status": "fill", "fill": "apply", "outcome": "installed"}],
            )
            proc = self.run_cli("--file", str(path), "--csv")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            rows = list(csv.reader(proc.stdout.splitlines()))
            self.assertEqual(rows[1][5], "apply")
            self.assertEqual(rows[1][6], "installed")

    def test_csv_respects_status_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(path, [{"harness": "a", "jev_status": "winner"}, {"harness": "b", "jev_status": "idf"}])
            proc = self.run_cli("--file", str(path), "--csv", "--status", "idf")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            rows = list(csv.reader(proc.stdout.splitlines()))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1][1], "b")

    def test_md_output(self):
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
                        "prompt_head": "a | b",
                    },
                ],
            )
            proc = self.run_cli("--file", str(path), "--md")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = proc.stdout.strip().splitlines()
            self.assertEqual(len(lines), 3)
            self.assertTrue(lines[0].startswith("| ts |"))
            self.assertIn("| fill | outcome |", lines[0])
            self.assertIn("---", lines[1])
            self.assertIn("beta", lines[2])
            self.assertIn("a \\| b", lines[2])

    def test_prune_by_harness(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"harness": "codex", "jev_status": "winner"},
                    {"harness": "grok", "jev_status": "idf"},
                    {"harness": "codex", "jev_status": "none"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--prune", "--harness", "codex")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(len(lines), 2)
            kept = [json.loads(l) for l in lines]
            self.assertTrue(all(e["harness"] == "codex" for e in kept))

    def test_statuses_sorted_desc(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"jev_status": "winner"},
                    {"jev_status": "idf"},
                    {"jev_status": "winner"},
                    {"jev_status": "fill"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--statuses")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                proc.stdout.strip().splitlines(), ["winner 2", "fill 1", "idf 1"]
            )

    def test_statuses_respects_harness_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"harness": "a", "jev_status": "winner"},
                    {"harness": "b", "jev_status": "idf"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--statuses", "--harness", "b")
            self.assertEqual(proc.stdout.strip(), "idf 1")

    def test_harnesses_sorted_desc(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"harness": "codex"},
                    {"harness": "grok"},
                    {"harness": "codex"},
                    {"jev_status": "x"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--harnesses")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                proc.stdout.strip().splitlines(), ["codex 2", "grok 1", "unknown 1"]
            )

    def test_winners_sorted_desc(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"winner": {"kind": "skill", "name": "a"}},
                    {"winner": {"kind": "skill", "name": "b"}},
                    {"winner": {"kind": "skill", "name": "a"}},
                    {"jev_status": "idf"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--winners")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip().splitlines(), ["skill:a 2", "skill:b 1"])

    def test_winners_empty_when_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(path, [{"jev_status": "idf"}])
            proc = self.run_cli("--file", str(path), "--winners")
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

    def test_outcome_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"outcome": "human"},
                    {"outcome": "blocked"},
                    {"outcome": "human"},
                    {"jev_status": "idf"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--outcome", "human", "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["total"], 2)

    def test_prune_by_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"outcome": "human"},
                    {"outcome": "blocked"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--prune", "--outcome", "blocked")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            entries = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["outcome"], "blocked")

    def test_fill_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"fill": "apply", "outcome": "human"},
                    {"fill": "peer", "outcome": "copied"},
                    {"fill": "apply", "outcome": "exists"},
                    {"jev_status": "idf"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--fill", "apply", "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["total"], 2)

    def test_fill_filter_combines_with_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"fill": "apply", "outcome": "human"},
                    {"fill": "apply", "outcome": "blocked"},
                ],
            )
            proc = self.run_cli(
                "--file", str(path), "--fill", "apply", "--outcome", "blocked", "--json"
            )
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["total"], 1)

    def test_outcomes_sorted_desc(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"outcome": "human"},
                    {"outcome": "blocked"},
                    {"outcome": "human"},
                    {"jev_status": "idf"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--outcomes")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                proc.stdout.strip().splitlines(), ["human 2", "blocked 1", "unknown 1"]
            )

    def test_fills_sorted_desc(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"fill": "apply"},
                    {"fill": "peer"},
                    {"fill": "apply"},
                    {"jev_status": "idf"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--fills")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                proc.stdout.strip().splitlines(), ["apply 2", "peer 1", "unknown 1"]
            )

    def test_field_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"harness": "codex", "custom": "x"},
                    {"harness": "codex", "custom": "y"},
                    {"harness": "grok", "custom": "x"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--field", "custom=x", "--json")
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["total"], 2)

    def test_field_filter_bad_spec_matches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(path, [{"harness": "codex"}])
            proc = self.run_cli("--file", str(path), "--field", "noequals", "--json")
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["total"], 0)

    def test_prune_no_filter_still_rc2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(path, [{"harness": "a"}])
            proc = self.run_cli("--file", str(path), "--prune")
            self.assertEqual(proc.returncode, 2)

    def test_md_respects_status_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(path, [{"harness": "a", "jev_status": "winner"}, {"harness": "b", "jev_status": "idf"}])
            proc = self.run_cli("--file", str(path), "--md", "--status", "idf")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = [l for l in proc.stdout.strip().splitlines() if "---" not in l]
            self.assertEqual(len(lines), 2)
            self.assertIn("| b |", lines[1])

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


class FilterHarnessTest(unittest.TestCase):
    run_cli = staticmethod(run_cli)

    def test_filter_harness(self):
        entries = [
            {"harness": "hermes", "jev_status": "winner"},
            {"harness": "codex", "jev_status": "none"},
            {"jev_status": "no-harness"},
        ]
        out = decisions.filter_harness(entries, "hermes")
        self.assertEqual([e["jev_status"] for e in out], ["winner"])
        self.assertIs(decisions.filter_harness(entries, ""), entries)

    def test_main_harness_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"harness": "hermes", "jev_status": "winner"},
                    {"harness": "codex", "jev_status": "none"},
                ],
            )
            proc = self.run_cli(
                "--file", str(path), "--json", "--harness", "codex"
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["total"], 1)
            self.assertEqual(stats["by_status"], {"none": 1})

    def test_filter_status(self):
        entries = [
            {"jev_status": "winner"},
            {"jev_status": "none"},
            {},
        ]
        self.assertEqual(len(decisions.filter_status(entries, "winner")), 1)
        self.assertEqual(len(decisions.filter_status(entries, "unknown")), 1)
        self.assertIs(decisions.filter_status(entries, ""), entries)

    def test_main_status_and_harness_combine(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"harness": "hermes", "jev_status": "winner"},
                    {"harness": "hermes", "jev_status": "none"},
                    {"harness": "codex", "jev_status": "winner"},
                ],
            )
            proc = self.run_cli(
                "--file", str(path), "--json", "--harness", "hermes",
                "--status", "winner",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["total"], 1)
            self.assertEqual(stats["by_status"], {"winner": 1})


class PruneTest(unittest.TestCase):
    run_cli = staticmethod(run_cli)

    def test_prune_keeps_window(self):
        import time as _time
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            now = _time.time()
            write_log(
                path,
                [
                    {"ts": now - 10 * 86400, "jev_status": "idf"},
                    {"ts": now - 100, "jev_status": "winner"},
                    "not-json",
                ],
            )
            proc = self.run_cli(
                "--file", str(path), "--days", "1", "--prune", "--json"
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("pruned 1 of 2", proc.stderr)
            kept = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0]["jev_status"], "winner")
            proc = self.run_cli("--file", str(path), "--json")
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["total"], 1)
            self.assertEqual(stats["bad_lines"], 0)

    def test_prune_requires_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(path, [{"ts": 1.0, "jev_status": "idf"}])
            proc = self.run_cli("--file", str(path), "--prune")
            self.assertEqual(proc.returncode, 2)
            self.assertIn("requires", proc.stderr)
            kept = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(kept), 1)

    def test_prune_entries_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(path, [{"a": 1}, {"b": "x"}])
            decisions.prune_entries(path, [{"b": "x"}])
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0]), {"b": "x"})
            leftovers = [p for p in Path(tmp).iterdir() if p.name != "decisions.jsonl"]
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()

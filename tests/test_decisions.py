import csv
import datetime
import json
import os
import re
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
        self.assertAlmostEqual(stats["latency_ms"]["mean"], 200.0, places=1)
        self.assertEqual(stats["top_winners"]["skill:alpha"], 2)
        self.assertIsNone(stats["first_ts"])

    def test_shortlist_n_aggregates(self):
        stats = decisions.summarize(
            [
                {"jev_status": "idf", "shortlist_n": 6},
                {"jev_status": "winner", "shortlist_n": 4},
                {"jev_status": "dedupe"},
            ]
        )
        self.assertEqual(stats["shortlist_n"]["n"], 2)
        self.assertAlmostEqual(stats["shortlist_n"]["mean"], 5.0, places=2)
        self.assertEqual(stats["shortlist_n"]["min"], 4.0)
        self.assertEqual(stats["shortlist_n"]["max"], 6.0)
        out = decisions.format_stats(stats)
        self.assertIn("shortlist_n:", out)

    def test_first_last_ts(self):
        entries = [
            {"ts": 1000.0, "jev_status": "winner"},
            {"ts": 3000, "jev_status": "none"},
            {"jev_status": "winner"},  # no ts
            {"ts": "bogus", "jev_status": "winner"},
            {"ts": True, "jev_status": "winner"},
            {"ts": 2000.5, "jev_status": "winner"},
        ]
        stats = decisions.summarize(entries)
        self.assertEqual(stats["first_ts"], 1000.0)
        self.assertEqual(stats["last_ts"], 3000.0)

    def test_span_line_humanizes_duration(self):
        stats = decisions.summarize(
            [
                {"ts": 1000.0, "jev_status": "winner"},
                {"ts": 1000.0 + 3 * 86400, "jev_status": "winner"},
            ]
        )
        self.assertIn("3.0d", decisions.format_stats(stats))
        short = decisions.summarize(
            [
                {"ts": 1000.0, "jev_status": "winner"},
                {"ts": 1000.0 + 7200, "jev_status": "winner"},
            ]
        )
        self.assertIn("2.0h", decisions.format_stats(short))

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

    def test_min_need_filters_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1700000000, "harness": "codex", "jev_status": "winner", "prompt_head": "high", "need": 0.9},
                    {"ts": 1700000001, "harness": "codex", "jev_status": "winner", "prompt_head": "low", "need": 0.2},
                    {"ts": 1700000002, "harness": "codex", "jev_status": "winner", "prompt_head": "noneed"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--min-need", "0.5", "--count")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)
            proc = self.run_cli(
                "--file", str(path), "--min-need", "0.5", "--jsonl"
            )
            lines = [l for l in proc.stdout.splitlines() if l.strip()]
            self.assertEqual(len(lines), 1)
            self.assertIn("high", lines[0])
            # env default
            proc = self.run_cli(
                "--file", str(path), "--count",
                env={"JEV_DECISIONS_MIN_NEED": "0.85"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)

    def test_drop_bad_rewrites_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            path.write_text(
                '{"ts":1,"jev_status":"winner","prompt_head":"a"}\n'
                "not json\n"
                '[1,2]\n'
                '{"ts":2,"jev_status":"none","prompt_head":"b"}\n',
                encoding="utf-8",
            )
            proc = self.run_cli("--file", str(path), "--drop-bad", "--count")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("dropped 2 bad line(s)", proc.stderr)
            lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(len(lines), 2)
            self.assertTrue(all(json.loads(l) for l in lines))
            _, bad = decisions.load_entries(path)
            self.assertEqual(bad, 0)
            # dry-run does not rewrite
            path.write_text("bad line\n{\"ts\":1}\n", encoding="utf-8")
            proc = self.run_cli("--file", str(path), "--drop-bad", "--dry-run")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("dry-run: would drop 1 bad line(s)", proc.stderr)
            self.assertIn("bad line", path.read_text(encoding="utf-8"))

    def test_min_latency_filters_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1700000000, "harness": "codex", "jev_status": "winner", "prompt_head": "fast", "latency_ms": 20},
                    {"ts": 1700000001, "harness": "codex", "jev_status": "winner", "prompt_head": "slow", "latency_ms": 900},
                    {"ts": 1700000002, "harness": "codex", "jev_status": "winner", "prompt_head": "nolat"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--min-latency", "100", "--count")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)
            proc = self.run_cli(
                "--file", str(path), "--count",
                env={"JEV_DECISIONS_MIN_LATENCY": "850"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)

    def test_winner_filters_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "jev_status": "winner", "winner": {"kind": "skill", "name": "jwt-auth"}, "prompt_head": "a"},
                    {"ts": 2, "jev_status": "winner", "winner": {"kind": "mcp", "name": "postgres"}, "prompt_head": "b"},
                    {"ts": 3, "jev_status": "none", "prompt_head": "c"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--winner", "jwt-auth", "--count")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)
            proc = self.run_cli("--file", str(path), "--winner", "mcp:postgres", "--count")
            self.assertIn("1", proc.stdout)
            proc = self.run_cli(
                "--file", str(path), "--count",
                env={"JEV_DECISIONS_WINNER": "jwt-auth"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)

    def test_explicit_filters_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "jev_status": "winner", "explicit": True, "prompt_head": "a"},
                    {"ts": 2, "jev_status": "winner", "explicit": False, "prompt_head": "b"},
                    {"ts": 3, "jev_status": "winner", "prompt_head": "c"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--explicit", "--count")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)
            proc = self.run_cli(
                "--file", str(path), "--count",
                env={"JEV_DECISIONS_EXPLICIT": "1"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)

    def test_question_filters_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "jev_status": "winner", "question": "load_tools", "prompt_head": "a"},
                    {"ts": 2, "jev_status": "winner", "question": "explicit", "prompt_head": "b"},
                    {"ts": 3, "jev_status": "none", "prompt_head": "c"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--question", "explicit", "--count")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)
            proc = self.run_cli(
                "--file", str(path), "--count",
                env={"JEV_DECISIONS_QUESTION": "load_tools"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)

    def test_dedupe_only_filters_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "jev_status": "dedupe", "dedupe": True, "prompt_head": "a"},
                    {"ts": 2, "jev_status": "winner", "dedupe": False, "prompt_head": "b"},
                    {"ts": 3, "jev_status": "winner", "prompt_head": "c"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--dedupe-only", "--count")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)
            proc = self.run_cli(
                "--file", str(path), "--count",
                env={"JEV_DECISIONS_DEDUPE": "1"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)

    def test_stale_filters_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "jev_status": "winner", "stale_sidecar": True, "prompt_head": "a"},
                    {"ts": 2, "jev_status": "winner", "stale_sidecar": False, "prompt_head": "b"},
                    {"ts": 3, "jev_status": "none", "prompt_head": "c"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--stale", "--count")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)
            proc = self.run_cli(
                "--file", str(path), "--count",
                env={"JEV_DECISIONS_STALE": "1"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)

    def test_sha_filters_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "jev_status": "winner", "prompt_sha": "abc123def", "prompt_head": "a"},
                    {"ts": 2, "jev_status": "winner", "prompt_sha": "fff999", "prompt_head": "b"},
                    {"ts": 3, "jev_status": "none", "prompt_head": "c"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--sha", "abc", "--count")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)
            proc = self.run_cli(
                "--file", str(path), "--count",
                env={"JEV_DECISIONS_SHA": "fff"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)

    def test_max_need_filters_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "jev_status": "winner", "need": 0.2, "prompt_head": "a"},
                    {"ts": 2, "jev_status": "winner", "need": 0.9, "prompt_head": "b"},
                    {"ts": 3, "jev_status": "none", "prompt_head": "c"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--max-need", "0.5", "--count")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)
            proc = self.run_cli(
                "--file", str(path), "--count",
                env={"JEV_DECISIONS_MAX_NEED": "0.5"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)

    def test_max_latency_filters_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "jev_status": "winner", "latency_ms": 10, "prompt_head": "a"},
                    {"ts": 2, "jev_status": "winner", "latency_ms": 9000, "prompt_head": "b"},
                    {"ts": 3, "jev_status": "none", "prompt_head": "c"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--max-latency", "100", "--count")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)
            proc = self.run_cli(
                "--file", str(path), "--count",
                env={"JEV_DECISIONS_MAX_LATENCY": "100"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)

    def test_over_budget_filters_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "jev_status": "winner", "over_budget": True, "prompt_head": "a"},
                    {"ts": 2, "jev_status": "winner", "over_budget": False, "prompt_head": "b"},
                    {"ts": 3, "jev_status": "none", "prompt_head": "c"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--over-budget", "--count")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)
            proc = self.run_cli(
                "--file", str(path), "--count",
                env={"JEV_DECISIONS_OVER_BUDGET": "1"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)

    def test_strong_filters_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "jev_status": "winner", "strong_pick": True, "prompt_head": "a"},
                    {"ts": 2, "jev_status": "winner", "strong_pick": False, "prompt_head": "b"},
                    {"ts": 3, "jev_status": "none", "prompt_head": "c"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--strong", "--count")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)
            proc = self.run_cli(
                "--file", str(path), "--count",
                env={"JEV_DECISIONS_STRONG": "1"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)

    def test_min_score_filters_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "jev_status": "winner", "shortlist_score_avg": 12.5, "prompt_head": "a"},
                    {"ts": 2, "jev_status": "winner", "shortlist_score_avg": 1.0, "prompt_head": "b"},
                    {"ts": 3, "jev_status": "none", "prompt_head": "c"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--min-score", "5", "--count")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)
            proc = self.run_cli(
                "--file", str(path), "--count",
                env={"JEV_DECISIONS_MIN_SCORE": "5"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)

    def test_min_catalog_filters_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "jev_status": "winner", "n_catalog": 40, "prompt_head": "a"},
                    {"ts": 2, "jev_status": "winner", "n_catalog": 3, "prompt_head": "b"},
                    {"ts": 3, "jev_status": "none", "prompt_head": "c"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--min-catalog", "10", "--count")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)
            proc = self.run_cli(
                "--file", str(path), "--count",
                env={"JEV_DECISIONS_MIN_CATALOG": "10"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)

    def test_reverse_lists_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "jev_status": "winner", "prompt_head": "first"},
                    {"ts": 2, "jev_status": "winner", "prompt_head": "last"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--jsonl")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            rows = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]
            self.assertEqual([r["prompt_head"] for r in rows], ["first", "last"])
            proc = self.run_cli("--file", str(path), "--jsonl", "--reverse")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            rows = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]
            self.assertEqual([r["prompt_head"] for r in rows], ["last", "first"])

    def test_min_shortlist_filters_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "jev_status": "winner", "shortlist_n": 12, "prompt_head": "a"},
                    {"ts": 2, "jev_status": "winner", "shortlist_n": 2, "prompt_head": "b"},
                    {"ts": 3, "jev_status": "none", "prompt_head": "c"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--min-shortlist", "6", "--count")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)
            proc = self.run_cli(
                "--file", str(path), "--count",
                env={"JEV_DECISIONS_MIN_SHORTLIST": "6"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)

    def test_min_prompt_len_filters_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "jev_status": "winner", "prompt_len": 400, "prompt_head": "a"},
                    {"ts": 2, "jev_status": "winner", "prompt_len": 12, "prompt_head": "b"},
                    {"ts": 3, "jev_status": "none", "prompt_head": "c"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--min-prompt-len", "100", "--count")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)
            proc = self.run_cli(
                "--file", str(path), "--count",
                env={"JEV_DECISIONS_MIN_PROMPT_LEN": "100"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("1", proc.stdout)

    def test_count_prints_filtered_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1700000000, "harness": "codex", "jev_status": "winner"},
                    {"ts": 1700000001, "harness": "hermes", "jev_status": "winner"},
                    {"ts": 1700000002, "harness": "codex", "jev_status": "fail_open"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--count")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "3")
            proc = self.run_cli(
                "--file", str(path), "--count", "--status", "winner"
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "2")

    def test_group_by_counts_nested_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "harness": "codex", "winner": {"kind": "skill", "name": "jwt-auth"}},
                    {"ts": 2, "harness": "codex"},
                    {"ts": 3, "harness": "hermes", "winner": {"kind": "skill", "name": "jwt-auth"}},
                ],
            )
            proc = self.run_cli("--file", str(path), "--group-by", "winner.name")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("jwt-auth 2", proc.stdout)
            self.assertIn("unknown 1", proc.stdout)
            proc = self.run_cli("--file", str(path), "--group-by", "harness", "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["counts"], {"codex": 2, "hermes": 1})
            self.assertEqual(payload["field"], "harness")

    def test_jq_prints_field_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "harness": "codex", "winner": {"kind": "skill", "name": "jwt-auth"}},
                    {"ts": 2, "harness": "codex"},
                    {"ts": 3, "harness": "hermes", "winner": {"kind": "mcp", "name": "sqlite"}},
                    {"ts": 4, "harness": "codex", "jev_status": "winner", "need": 0.5},
                ],
            )
            proc = self.run_cli("--file", str(path), "--jq", "winner.name")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.splitlines(), ["jwt-auth", "null", "sqlite", "null"])
            proc = self.run_cli("--file", str(path), "--jq", "harness", "--status", "winner")
            self.assertEqual(proc.stdout.splitlines(), ["codex"])
            proc = self.run_cli("--file", str(path), "--jq", "winner", "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["field"], "winner")
            self.assertEqual(payload["values"][0], {"kind": "skill", "name": "jwt-auth"})
            self.assertIsNone(payload["values"][1])
            proc = self.run_cli("--file", str(path), "--jq", "need")
            self.assertEqual(proc.stdout.splitlines(), ["null", "null", "null", "0.5"])

    def test_tail_env_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {
                        "ts": 1700000000,
                        "jev_status": "winner",
                        "prompt_head": "alpha-entry",
                    },
                    {
                        "ts": 1700000001,
                        "jev_status": "winner",
                        "prompt_head": "beta-entry",
                    },
                ],
            )
            proc = self.run_cli(
                "--file", str(path), env={"JEV_DECISIONS_TAIL": "1"}
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            shown = [
                l for l in proc.stdout.splitlines() if re.match(r"^\d{2}-\d{2} ", l)
            ]
            self.assertEqual(len(shown), 1)
            self.assertIn("beta-entry", shown[0])
            proc = self.run_cli(
                "--file", str(path), "--tail", "2", env={"JEV_DECISIONS_TAIL": "1"}
            )
            shown = [
                l for l in proc.stdout.splitlines() if re.match(r"^\d{2}-\d{2} ", l)
            ]
            self.assertEqual(len(shown), 2)

    def test_first_env_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {
                        "ts": 1700000000,
                        "jev_status": "winner",
                        "prompt_head": "alpha-entry",
                    },
                    {
                        "ts": 1700000001,
                        "jev_status": "winner",
                        "prompt_head": "beta-entry",
                    },
                ],
            )
            proc = self.run_cli(
                "--file", str(path), env={"JEV_DECISIONS_FIRST": "1"}
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            shown = [
                l for l in proc.stdout.splitlines() if re.match(r"^\d{2}-\d{2} ", l)
            ]
            self.assertEqual(len(shown), 1)
            self.assertIn("alpha-entry", shown[0])

    def test_top_env_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"jev_status": "winner"},
                    {"jev_status": "none"},
                    {"jev_status": "none"},
                    {"jev_status": "error"},
                ],
            )
            proc = self.run_cli(
                "--file", str(path), "--statuses", env={"JEV_DECISIONS_TOP": "1"}
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            rows = [l for l in proc.stdout.splitlines() if l.strip()]
            self.assertEqual(len(rows), 1)
            self.assertIn("none 2", rows[0])
            proc = self.run_cli(
                "--file",
                str(path),
                "--statuses",
                "--top",
                "3",
                env={"JEV_DECISIONS_TOP": "1"},
            )
            self.assertEqual(len([l for l in proc.stdout.splitlines() if l.strip()]), 3)

    def test_days_env_default(self):
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1700000000, "jev_status": "winner"},
                    {"ts": _time.time(), "jev_status": "winner"},
                ],
            )
            proc = self.run_cli(
                "--file", str(path), env={"JEV_DECISIONS_DAYS": "1"}
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("entries: 1", proc.stdout)
            proc = self.run_cli(
                "--file", str(path), "--days", "0", env={"JEV_DECISIONS_DAYS": "1"}
            )
            self.assertIn("entries: 2", proc.stdout)

    def test_filter_env_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"jev_status": "winner", "harness": "hermes"},
                    {"jev_status": "none", "harness": "codex"},
                ],
            )
            proc = self.run_cli(
                "--file", str(path), env={"JEV_DECISIONS_STATUS": "none"}
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("entries: 1", proc.stdout)
            proc = self.run_cli(
                "--file",
                str(path),
                "--status",
                "winner",
                env={"JEV_DECISIONS_STATUS": "none"},
            )
            self.assertIn("entries: 1", proc.stdout)

    def test_since_until_env_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 100, "jev_status": "winner"},
                    {"ts": 200, "jev_status": "winner"},
                    {"ts": 300, "jev_status": "winner"},
                ],
            )
            proc = self.run_cli(
                "--file", str(path), env={"JEV_DECISIONS_SINCE": "150"}
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("entries: 2", proc.stdout)
            proc = self.run_cli(
                "--file", str(path), env={"JEV_DECISIONS_UNTIL": "150"}
            )
            self.assertIn("entries: 1", proc.stdout)
            proc = self.run_cli(
                "--file", str(path), "--since", "", env={"JEV_DECISIONS_SINCE": "150"}
            )
            self.assertIn("entries: 3", proc.stdout)

    def test_stats_iso_fields(self):
        entries = [{"ts": 1700000000}, {"ts": 1700086400}]
        stats = decisions.summarize(entries)
        self.assertEqual(stats["first_iso"], "2023-11-14T22:13:20Z")
        self.assertEqual(stats["last_iso"], "2023-11-15T22:13:20Z")
        empty = decisions.summarize([])
        self.assertIsNone(empty["first_iso"])
        self.assertIsNone(empty["last_iso"])

    def test_errors_lists_bad_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            path.write_text(
                '{"ts": 1}\nnot json\n[1, 2]\n{"ts": 2}\n',
                encoding="utf-8",
            )
            proc = self.run_cli("--file", str(path), "--errors")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("2: not json", proc.stdout)
            self.assertIn("3: [1, 2]", proc.stdout)
            self.assertNotIn('"ts": 1', proc.stdout)

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

    def test_prune_drops_bad_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            path.write_text(
                '{"harness": "codex", "jev_status": "winner"}\nnot json\n{"harness": "grok", "jev_status": "idf"}\n',
                encoding="utf-8",
            )
            proc = self.run_cli("--file", str(path), "--prune", "--harness", "codex")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("dropped 1 bad line(s)", proc.stderr)
            lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["harness"], "codex")

    def test_prompt_filter_matches_head_and_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "prompt_head": "fix the flaky test", "prompt_tail": "tail-a"},
                    {"ts": 2, "prompt_head": "add oauth", "prompt_tail": "tail-b"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--prompt", "flaky", "--count")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "1")
            proc = self.run_cli("--file", str(path), "--prompt", "TAIL-B", "--count")
            self.assertEqual(proc.stdout.strip(), "1")

    def test_out_writes_filtered_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            out = Path(tmp) / "sub.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "harness": "codex", "jev_status": "winner"},
                    {"ts": 2, "harness": "hermes", "jev_status": "winner"},
                    {"ts": 3, "harness": "codex", "jev_status": "fail_open"},
                ],
            )
            proc = self.run_cli(
                "--file", str(path), "--out", str(out), "--harness", "codex"
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("wrote 2 entries", proc.stderr)
            rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(r["harness"] == "codex" for r in rows))

    def test_prune_dry_run_keeps_file(self):
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
            proc = self.run_cli(
                "--file", str(path), "--prune", "--dry-run", "--harness", "codex"
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("dry-run: would prune 1 of 3 entries", proc.stderr)
            lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(len(lines), 3)

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

    def test_jsonl_prints_raw_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"harness": "a", "ts": 1},
                    {"harness": "b", "outcome": "human"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--jsonl")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = proc.stdout.strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["harness"], "a")
            self.assertEqual(json.loads(lines[1])["outcome"], "human")

    def test_jsonl_respects_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [{"harness": "a"}, {"harness": "b"}],
            )
            proc = self.run_cli(
                "--file", str(path), "--jsonl", "--harness", "a"
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = proc.stdout.strip().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["harness"], "a")

    def test_until_filters_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"harness": "old", "ts": 100},
                    {"harness": "new", "ts": 200},
                ],
            )
            proc = self.run_cli(
                "--file", str(path), "--until", "150", "--json"
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["filtered"], 1)
            self.assertEqual(stats["until"], 150.0)

    def test_until_with_since_is_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [{"harness": "a", "ts": 100}, {"harness": "b", "ts": 200}, {"harness": "c", "ts": 300}],
            )
            proc = self.run_cli(
                "--file", str(path), "--since", "150", "--until", "250", "--json"
            )
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["filtered"], 1)

    def test_prune_by_until(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(path, [{"harness": "a", "ts": 100}, {"harness": "b", "ts": 300}])
            proc = self.run_cli(
                "--file", str(path), "--prune", "--until", "200"
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            kept = [json.loads(l) for l in path.read_text().splitlines()]
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0]["harness"], "a")

    def test_first_prints_earliest_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"harness": "first", "ts": 1},
                    {"harness": "mid", "ts": 2},
                    {"harness": "last", "ts": 3},
                ],
            )
            proc = self.run_cli("--file", str(path), "--first", "1")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("first", proc.stdout)
            self.assertNotIn("last", proc.stdout.splitlines()[-1])

    def test_first_and_tail_first_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [{"harness": "h1", "ts": 1}, {"harness": "h9", "ts": 9}],
            )
            proc = self.run_cli(
                "--file", str(path), "--first", "1", "--tail", "1"
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            tail_lines = [l for l in proc.stdout.splitlines() if "h" in l]
            self.assertIn("h1", proc.stdout)

    def test_dedupes_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"harness": "a", "dedupe": True},
                    {"harness": "b", "dedupe": True},
                    {"harness": "c"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--dedupes")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = dict(
                line.rsplit(" ", 1) for line in proc.stdout.strip().splitlines()
            )
            self.assertEqual(lines["True"], "2")
            self.assertEqual(lines["unknown"], "1")

    def test_fields_lists_all_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"harness": "a", "ts": 1},
                    {"harness": "b", "outcome": "human"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--fields")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = dict(
                line.rsplit(" ", 1) for line in proc.stdout.strip().splitlines()
            )
            self.assertEqual(lines["harness"], "2")
            self.assertEqual(lines["ts"], "1")
            self.assertEqual(lines["outcome"], "1")

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

    def test_since_until_accept_iso8601(self):
        import time as _time
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            now = _time.time()
            write_log(
                path,
                [
                    {"ts": now - 7200, "jev_status": "idf"},
                    {"ts": now - 60, "jev_status": "winner"},
                ],
            )
            iso = datetime.datetime.fromtimestamp(
                now - 3600, tz=datetime.timezone.utc
            ).isoformat()
            proc = self.run_cli("--file", str(path), "--json", "--since", iso)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["total"], 1)
            self.assertEqual(stats["by_status"]["winner"], 1)
            bad = self.run_cli("--file", str(path), "--since", "not-a-time")
            self.assertEqual(bad.returncode, 2)

    def test_env_decisions_overrides_default_path(self):
        import time as _time
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(path, [{"ts": _time.time(), "jev_status": "winner"}])
            proc = self.run_cli("--json", env={"JEV_DECISIONS": str(path)})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["total"], 1)
            self.assertEqual(stats["by_status"]["winner"], 1)

    def test_week_alias_filters_last_7_days(self):
        import time as _time
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            now = _time.time()
            write_log(
                path,
                [
                    {"ts": now - 8 * 86400, "jev_status": "idf"},
                    {"ts": now - 6 * 86400, "jev_status": "winner"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--json", "--week")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["total"], 1)
            self.assertEqual(stats["by_status"]["winner"], 1)
            self.assertIsNotNone(stats["since"])

    def test_top_caps_count_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "jev_status": "winner"},
                    {"ts": 2, "jev_status": "winner"},
                    {"ts": 3, "jev_status": "idf"},
                    {"ts": 4, "jev_status": "none"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--statuses", "--top", "1")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = proc.stdout.strip().splitlines()
            self.assertEqual(lines, ["winner 2"])

    def test_top_zero_no_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [{"ts": i, "jev_status": s} for i, s in enumerate(("a", "b", "c"))],
            )
            proc = self.run_cli("--file", str(path), "--statuses", "--top", "0")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(proc.stdout.strip().splitlines()), 3)

    def test_counts_json_emits_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "jev_status": "winner"},
                    {"ts": 2, "jev_status": "winner"},
                    {"ts": 3, "jev_status": "idf"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--statuses", "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out, {"counts": {"winner": 2, "idf": 1}})

    def test_field_dotted_digs_nested(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "jev_status": "winner", "winner": {"kind": "skill", "name": "jwt-auth"}},
                    {"ts": 2, "jev_status": "winner", "winner": {"kind": "skill", "name": "ascii-art"}},
                    {"ts": 3, "jev_status": "none"},
                ],
            )
            proc = self.run_cli(
                "--file", str(path), "--field", "winner.name=jwt-auth", "--json"
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["total"], 1)

    def test_field_dotted_missing_leaf_no_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(path, [{"ts": 1, "jev_status": "none"}])
            proc = self.run_cli(
                "--file", str(path), "--field", "winner.name=x", "--json"
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["total"], 0)

    def test_days_overrides_week(self):
        import time as _time
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            now = _time.time()
            write_log(
                path,
                [
                    {"ts": now - 6 * 86400, "jev_status": "idf"},
                    {"ts": now - 100, "jev_status": "winner"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--json", "--week", "--days", "1")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["total"], 1)
            self.assertEqual(stats["by_status"]["winner"], 1)


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

    def test_where_filters_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "harness": "claude", "jev_status": "ok", "winner": "skill_jwt-auth"},
                    {"ts": 2, "harness": "codex", "jev_status": "ok", "winner": "skill_zzz"},
                    {"ts": 3, "harness": "claude", "jev_status": "ok"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--where", "winner=skill_jwt-auth", "--count")
            self.assertEqual(proc.returncode, 0)
            self.assertIn("1", proc.stdout)
            proc = self.run_cli("--file", str(path), "--where", "harness=CLAUDE", "--where", "jev_status=ok", "--count")
            self.assertEqual(proc.returncode, 0)
            self.assertIn("2", proc.stdout)

    def test_where_not_filters_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "harness": "claude", "jev_status": "ok", "winner": "skill_jwt-auth"},
                    {"ts": 2, "harness": "codex", "jev_status": "ok", "winner": "skill_zzz"},
                    {"ts": 3, "harness": "claude", "jev_status": "ok"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--where-not", "winner=skill_jwt-auth", "--count")
            self.assertEqual(proc.returncode, 0)
            self.assertIn("2", proc.stdout)
            proc = self.run_cli(
                "--file", str(path), "--where", "harness=claude", "--where-not", "winner=skill_jwt-auth", "--count"
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("1", proc.stdout)

    def test_out_honors_csv_and_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(path, [{"ts": 1, "harness": "claude", "jev_status": "ok"}])
            csv_path = Path(tmp) / "out.csv"
            proc = self.run_cli("--file", str(path), "--csv", "--out", str(csv_path))
            self.assertEqual(proc.returncode, 0)
            text = csv_path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("ts,harness,jev_status"))
            self.assertIn("claude", text)
            md_path = Path(tmp) / "out.md"
            proc = self.run_cli("--file", str(path), "--md", "--out", str(md_path))
            self.assertEqual(proc.returncode, 0)
            text = md_path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("| ts | harness"))
            jsonl_path = Path(tmp) / "out.jsonl"
            proc = self.run_cli("--file", str(path), "--out", str(jsonl_path))
            self.assertEqual(proc.returncode, 0)
            line = jsonl_path.read_text(encoding="utf-8").strip()
            self.assertEqual(json.loads(line)["harness"], "claude")

    def test_jq_uniq_dedupes_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "harness": "claude"},
                    {"ts": 2, "harness": "codex"},
                    {"ts": 3, "harness": "claude"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--jq", "harness", "--uniq")
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.split(), ["claude", "codex"])
            proc = self.run_cli("--file", str(path), "--jq", "harness")
            self.assertEqual(proc.stdout.split(), ["claude", "codex", "claude"])

    def test_last_prints_newest_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            write_log(
                path,
                [
                    {"ts": 1, "harness": "claude", "jev_status": "ok"},
                    {"ts": 2, "harness": "codex", "jev_status": "ok"},
                ],
            )
            proc = self.run_cli("--file", str(path), "--last")
            self.assertEqual(proc.returncode, 0)
            entry = json.loads(proc.stdout)
            self.assertEqual(entry["harness"], "codex")
            proc = self.run_cli("--file", str(path), "--last", "--harness", "claude")
            entry = json.loads(proc.stdout)
            self.assertEqual(entry["harness"], "claude")
            proc = self.run_cli("--file", str(path), "--last", "--status", "nope")
            self.assertEqual(proc.stdout.strip(), "")

if __name__ == "__main__":
    unittest.main()

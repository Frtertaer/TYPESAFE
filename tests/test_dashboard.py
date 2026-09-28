#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""dashboard.py — self-contained ASCII dashboard.html from the ops artifacts.

Fixtures mirror the live-eval workflow outputs: a decisions.jsonl routing
log, eval-history.jsonl run records, and the eval-live.json compare
payload carrying the Cyrillic drift flags. Asserts the file renders on
empty/minimal logs, stays pure-ASCII (lesson from #21), and that the
--watch loop is bounded by --max-ticks / JEV_DASHBOARD_WATCH_* knobs.
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
sys.path.insert(0, str(SCRIPTS))

import dashboard


def run_cli(*args: str, env_add: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_add or {})
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "dashboard.py"), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def write_jsonl(path: Path, rows: list) -> None:
    path.write_text(
        "\n".join(json.dumps(r) if isinstance(r, dict) else r for r in rows)
        + "\n",
        encoding="utf-8",
    )


def entry(i: int, **over) -> dict:
    e = {
        "ts": 1700000000.0 + i * 60,
        "schema": 2,
        "harness": "hermes",
        "prompt_sha": "sha%08d" % i,
        "prompt_head": "pick a skill %d" % i,
        "jev_status": "winner",
        "winner": {"kind": "skill", "name": "demo"},
        "probabilities": {"demo": 0.9, "other": 0.1},
        "pick_confidence": 0.9,
        "strong_pick": True,
        "latency_ms": 400 + i,
        "budget_ms": 12000,
    }
    e.update(over)
    return e


def history_rec(ts: int, verdict: str = "PASS", streaks=None) -> dict:
    return {
        "ts": ts,
        "run_url": "https://example.test/runs/%d" % ts,
        "verdict": verdict,
        "worst_noul": 0.8,
        "ab_mean_delta": 0.02,
        "streaks": streaks or {},
    }


class RenderTest(unittest.TestCase):
    def test_empty_log_renders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            log.write_text("", encoding="utf-8")
            out = Path(tmp) / "dashboard.html"
            proc = run_cli("--file", str(log), "--out", str(out))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            text = out.read_text(encoding="utf-8")
            self.assertIn("dashboard", text.lower())
            self.assertIn("pick rate", text)
            # no `promoted` field anywhere -> section skipped
            self.assertNotIn("promoted (", text)

    def test_minimal_log_has_key_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            write_jsonl(
                log,
                [
                    entry(0, promoted=True),
                    entry(1, jev_status="escalate",
                        escalate_reason="low_confidence",
                        winner=None, strong_pick=False, promoted=False),
                ],
            )
            hist = Path(tmp) / "eval-history.jsonl"
            write_jsonl(
                hist,
                [
                    history_rec(1700000000),
                    history_rec(1700604800, verdict="FAIL",
                                streaks={"case-a": 3}),
                ],
            )
            eval_json = Path(tmp) / "eval-live.json"
            eval_json.write_text(
                json.dumps(
                    {
                        "drift": {
                            "streaks": {"case-a": 3},
                            "flags": ["case case-a below gate"],
                            "fails": ["case-a ниже гейта 3"],
                            "warn_weeks": 2,
                            "fail_weeks": 3,
                        }
                    }
                ),
                encoding="utf-8",
            )
            out = Path(tmp) / "dashboard.html"
            proc = run_cli(
                "--file", str(log),
                "--history", str(hist),
                "--eval", str(eval_json),
                "--out", str(out),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            text = out.read_text(encoding="utf-8")
            for marker in (
                "pick rate",
                "applied rate",
                "override rate",
                "miss rate",
                "promoted",
                "Escalate reasons",
                "Weekly eval trend",
                "case-a",
                "fail-gate",
            ):
                self.assertIn(marker, text, marker)

    def test_output_is_pure_ascii(self) -> None:
        """Cyrillic drift flags must survive as entities, never bytes."""
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            write_jsonl(log, [entry(0)])
            eval_json = Path(tmp) / "eval-live.json"
            eval_json.write_text(
                json.dumps(
                    {
                        "drift": {
                            "streaks": {"кейс": 5},
                            "flags": ["кейс ниже гейта"],
                            "fails": [],
                            "warn_weeks": 2,
                            "fail_weeks": 3,
                        }
                    }
                ),
                encoding="utf-8",
            )
            out = Path(tmp) / "dashboard.html"
            proc = run_cli(
                "--file", str(log), "--eval", str(eval_json), "--out", str(out)
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            raw = out.read_bytes()
            self.assertTrue(
                all(b < 128 for b in raw), "dashboard.html is not pure ASCII"
            )
            self.assertIn("&#", raw.decode("ascii"))

    def test_stdout_dash_prints_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            write_jsonl(log, [entry(0)])
            proc = run_cli("--file", str(log), "--out", "-")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("<html", proc.stdout)


class JsonSurfaceTest(unittest.TestCase):
    def test_json_payload_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            write_jsonl(log, [entry(0), "bad-line"])
            hist = Path(tmp) / "eval-history.jsonl"
            write_jsonl(hist, [history_rec(1700000000)])
            proc = run_cli(
                "--file", str(log), "--history", str(hist), "--json",
                "--out", str(Path(tmp) / "d.html"),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["entries"], 1)
            self.assertEqual(payload["bad_lines"], 1)
            self.assertEqual(payload["verdict"], "PASS")
            self.assertEqual(len(payload["weekly"]), 1)
            self.assertIn("pick_rate", payload["acceptance"])
            self.assertIn("p50", payload["latency_ms"])

    def test_jq_digs_and_rc2_on_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            write_jsonl(log, [entry(0)])
            proc = run_cli("--file", str(log), "--jq", "entries")
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(json.loads(proc.stdout), 1)
            bad = run_cli("--file", str(log), "--jq", "nope.nope")
            self.assertEqual(bad.returncode, 2)

    def test_schema_and_env(self) -> None:
        proc = run_cli("--schema")
        self.assertEqual(proc.returncode, 0)
        self.assertIn(":", proc.stdout.splitlines()[0])
        env = run_cli("--env")
        self.assertEqual(env.returncode, 0)
        report = json.loads(env.stdout)
        for key in ("file", "history", "eval", "out", "watch_max",
                    "watch_secs", "watch_quiet"):
            self.assertIn(key, report)
        env_jq = run_cli("--env", "--jq", "out")
        self.assertEqual(env_jq.returncode, 0)

    def test_verdict_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            write_jsonl(log, [entry(0)])
            verdict = Path(tmp) / "verdict.json"
            proc = run_cli(
                "--file", str(log),
                "--out", str(Path(tmp) / "d.html"),
                "--verdict", str(verdict),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "ok")


class WatchTest(unittest.TestCase):
    def test_watch_bounded_by_max_ticks(self) -> None:
        """--watch S --max-ticks N emits exactly N tick lines and exits 0."""
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            write_jsonl(log, [entry(0)])
            out = Path(tmp) / "dashboard.html"
            proc = run_cli(
                "--file", str(log), "--out", str(out),
                "--watch", "0.05", "--max-ticks", "2",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            ticks = [
                json.loads(l) for l in proc.stdout.splitlines() if l.strip()
            ]
            self.assertEqual(len(ticks), 2)
            self.assertTrue(out.is_file())
            self.assertTrue(all(t.get("entries") == 1 for t in ticks))

    def test_watch_env_caps_bind(self) -> None:
        """JEV_DASHBOARD_WATCH_MAX caps ticks; _SECS bounds the loop;
        _QUIET suppresses clean ticks (regenerates on new records)."""
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            write_jsonl(log, [entry(0)])
            out = Path(tmp) / "dashboard.html"
            proc = run_cli(
                "--file", str(log), "--out", str(out),
                "--watch", "0.05",
                env_add={
                    "JEV_DASHBOARD_WATCH_MAX": "1",
                    "JEV_DASHBOARD_WATCH_SECS": "5",
                    "JEV_DASHBOARD_WATCH_QUIET": "0",
                },
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            ticks = [
                json.loads(l) for l in proc.stdout.splitlines() if l.strip()
            ]
            self.assertEqual(len(ticks), 1)

    def test_watch_flag_beats_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            write_jsonl(log, [entry(0)])
            proc = run_cli(
                "--file", str(log),
                "--out", str(Path(tmp) / "d.html"),
                "--watch", "0.05", "--max-ticks", "2",
                env_add={"JEV_DASHBOARD_WATCH_MAX": "9"},
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(len(proc.stdout.strip().splitlines()), 2)

    def test_unchanged_max_stops_idle_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            write_jsonl(log, [entry(0)])
            proc = run_cli(
                "--file", str(log),
                "--out", str(Path(tmp) / "d.html"),
                "--watch", "0.05", "--unchanged-max", "2",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            ticks = proc.stdout.strip().splitlines()
            # first tick differs (no prev), then identical ticks stop it
            self.assertLessEqual(len(ticks), 3)
            self.assertGreaterEqual(len(ticks), 2)


class ModuleSurfaceTest(unittest.TestCase):
    def test_self_test(self) -> None:
        proc = run_cli("--self-test")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("self-test: ok", proc.stdout)

    def test_no_selftest_files_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            before = set(os.listdir(tmp))
            subprocess.run(
                [sys.executable, str(SCRIPTS / "dashboard.py"), "--self-test"],
                capture_output=True, text=True, timeout=60, cwd=tmp,
            )
            self.assertEqual(set(os.listdir(tmp)), before)

    def test_help_rc0(self) -> None:
        proc = run_cli("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("usage", proc.stdout.lower())

    def test_missing_inputs_still_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "dashboard.html"
            proc = run_cli(
                "--file", str(Path(tmp) / "nope.jsonl"),
                "--history", str(Path(tmp) / "nope2.jsonl"),
                "--eval", str(Path(tmp) / "nope3.json"),
                "--out", str(out),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(out.is_file())


if __name__ == "__main__":
    unittest.main()

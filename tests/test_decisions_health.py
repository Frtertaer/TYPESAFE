#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""decisions.py --harness-health: per-harness per-hour timeout/error
rates — the cheap quota signal over decisions.jsonl rows."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "decisions_health",
    ROOT / "skills" / "jev-consult" / "scripts" / "decisions.py",
)
DEC = importlib.util.module_from_spec(SPEC)
sys.modules["decisions_health"] = DEC
SPEC.loader.exec_module(DEC)

LOG = ROOT / "tests" / "fixtures" / "decisions-health.jsonl"

H10 = 1700000000.0          # 2023-11-14T22:13Z -> window 22:00Z
H10SAME = H10 + 300.0       # same hour
H11 = H10 + 3600.0          # next hour


def run_main(argv):
    buf = io.StringIO()
    with patch.dict(
        os.environ, {"JEV_CONSULT_LOG": "0"}
    ), patch.object(sys, "stdout", buf):
        rc = DEC.main(argv)
    return rc, buf.getvalue()


def write_log(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )


def entry(harness: str, status: str, ts: float) -> dict:
    return {
        "ts": ts,
        "harness": harness,
        "jev_status": status,
        "prompt_head": "x",
    }


class HarnessHealthTests(unittest.TestCase):
    def test_buckets_per_harness_per_hour(self) -> None:
        rows = DEC.harness_health(
            [
                entry("codex", "winner", H10),
                entry("codex", "timeout", H10SAME),
                entry("codex", "error", H11),
                entry("grok", "winner", H10),
            ]
        )
        self.assertEqual(len(rows), 3)
        codex_h1 = next(
            r for r in rows if r["harness"] == "codex" and r["entries"] == 2
        )
        self.assertEqual(codex_h1["window"], "2023-11-14T22:00Z")
        self.assertEqual(codex_h1["timeouts"], 1)
        self.assertEqual(codex_h1["timeout_rate"], 0.5)
        self.assertEqual(codex_h1["error_rate"], 0.0)
        codex_h2 = next(
            r for r in rows if r["harness"] == "codex" and r["entries"] == 1
        )
        self.assertEqual(codex_h2["window"], "2023-11-14T23:00Z")
        self.assertEqual(codex_h2["error_rate"], 1.0)

    def test_rates_use_attempted_calls_not_all_entries(self) -> None:
        """dedupe/idf records never reached Jev — they must not dilute
        or inflate the timeout/error denominator."""
        rows = DEC.harness_health(
            [
                entry("codex", "winner", H10),
                entry("codex", "dedupe", H10SAME),
                entry("codex", "idf", H10SAME),
                entry("codex", "timeout", H10SAME),
            ]
        )
        (row,) = rows
        self.assertEqual(row["entries"], 4)
        self.assertEqual(row["attempted"], 2)
        self.assertEqual(row["timeout_rate"], 0.5)

    def test_no_attempted_calls_gives_zero_rates(self) -> None:
        rows = DEC.harness_health([entry("codex", "dedupe", H10)])
        self.assertEqual(rows[0]["attempted"], 0)
        self.assertEqual(rows[0]["error_rate"], 0.0)
        self.assertEqual(rows[0]["timeout_rate"], 0.0)

    def test_replayed_and_sidecar_records_are_not_attempts(self) -> None:
        """A dedupe-replayed or explicit 'winner' never reached Jev; fill /
        budget / empty records likewise. Only the real timeout counts."""
        rows = DEC.harness_health(
            [
                entry("codex", "timeout", H10),
                dict(entry("codex", "winner", H10SAME), dedupe=True, question="dedupe"),
                dict(entry("codex", "winner", H10SAME), explicit=True, question="explicit"),
                dict(entry("codex", "winner", H10SAME), question="env"),
                entry("codex", "fill", H10SAME),
                entry("codex", "budget", H10SAME),
                entry("codex", "empty", H10SAME),
            ]
        )
        (row,) = rows
        self.assertEqual(row["entries"], 7)
        self.assertEqual(row["attempted"], 1)
        self.assertEqual(row["timeouts"], 1)
        self.assertEqual(row["timeout_rate"], 1.0)

    def test_real_call_statuses_still_count(self) -> None:
        """winner/none from a real Jev answer keep counting as attempts."""
        rows = DEC.harness_health(
            [
                dict(entry("codex", "winner", H10), question="load_tools"),
                entry("codex", "none", H10SAME),
                entry("codex", "error", H10SAME),
            ]
        )
        (row,) = rows
        self.assertEqual(row["attempted"], 3)
        self.assertEqual(row["error_rate"], round(1 / 3, 4))

    def test_missing_ts_buckets_as_unknown(self) -> None:
        rows = DEC.harness_health([{"harness": "grok", "jev_status": "error"}])
        self.assertEqual(rows[0]["window"], "unknown")
        self.assertEqual(rows[0]["error_rate"], 1.0)

    def test_empty_log_has_no_rows(self) -> None:
        self.assertEqual(DEC.harness_health([]), [])

    def test_cli_text_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "d.jsonl"
            write_log(
                log,
                [
                    entry("codex", "timeout", H10),
                    entry("codex", "winner", H10SAME),
                ],
            )
            rc, text = run_main(
                ["--file", str(log), "--harness-health"]
            )
            self.assertEqual(rc, 0)
            self.assertIn("codex", text)
            self.assertIn("2023-11-14T22:00Z", text)
            self.assertIn("0.5", text)
            rc, text = run_main(
                ["--file", str(log), "--harness-health", "--json"]
            )
            data = json.loads(text)
            row = data["harness_health"][0]
            self.assertEqual(row["timeout_rate"], 0.5)
            self.assertEqual(row["error_rate"], 0.0)

    def test_jsonl_and_csv_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "d.jsonl"
            write_log(log, [entry("grok", "error", H10)])
            rc, text = run_main(
                ["--file", str(log), "--harness-health", "--jsonl"]
            )
            rows = [json.loads(l) for l in text.splitlines() if l.strip()]
            self.assertEqual(rows[0]["harness"], "grok")
            self.assertEqual(rows[0]["error_rate"], 1.0)
            rc, text = run_main(
                ["--file", str(log), "--harness-health", "--csv"]
            )
            self.assertIn("harness", text.splitlines()[0])
            self.assertIn("grok", text)

    def test_empty_log_prints_no_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "d.jsonl"
            log.write_text("", encoding="utf-8")
            rc, text = run_main(
                ["--file", str(log), "--harness-health"]
            )
            self.assertEqual(rc, 0)
            self.assertIn("no entries", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

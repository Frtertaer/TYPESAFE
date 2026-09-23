"""--dry-run never mutates the target file, on every mutating op.

Each probe snapshots the file's bytes, runs the operation under
--dry-run, and asserts byte-identity. A dry run that writes is a silent
data bug — the operator asked for a preview, not a change.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "skills" / "jev-consult" / "scripts"


def _run(name: str, argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / name), *argv],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


class DryRunParityTests(unittest.TestCase):
    def test_fix_dry_run_writes_nothing(self) -> None:
        """--fix --dry-run on a fixable file leaves its bytes untouched."""
        with tempfile.TemporaryDirectory() as tmp:
            # policy_lint: a policy missing a sortable/no-op fixable detail —
            # use a policy with an unknown key ordering issue if any; the
            # simplest fixable fixture is a policy missing 'version'.
            bad_policy = Path(tmp) / "policy.json"
            bad_policy.write_text(
                json.dumps({"thresholds": {}, "extra_top_level_key": 1}),
                encoding="utf-8",
            )
            before = bad_policy.read_bytes()
            proc = _run(
                "policy_lint.py", [str(bad_policy), "--fix", "--dry-run"]
            )
            self.assertIn(proc.returncode, (0, 1))
            self.assertEqual(bad_policy.read_bytes(), before)

            # trigger_lint: a cases file with a fixable field
            bad_cases = Path(tmp) / "cases.json"
            bad_cases.write_text(
                json.dumps({"cases": [{"name": "x", "extra": 1}]}),
                encoding="utf-8",
            )
            before = bad_cases.read_bytes()
            proc = _run(
                "trigger_lint.py", [str(bad_cases), "--fix", "--dry-run"]
            )
            self.assertIn(proc.returncode, (0, 1, 2))
            self.assertEqual(bad_cases.read_bytes(), before)

    def test_prune_and_fill_gaps_dry_run_write_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # trace prune --dry-run: trace file unchanged
            trace = Path(tmp) / "t.trace.json"
            _run("trace.py", ["--file", str(trace), "init", "--plan", "x"])
            _run("trace.py", ["--file", str(trace), "bump"])
            before = trace.read_bytes()
            proc = _run(
                "trace.py",
                ["--file", str(trace), "prune", "--older-than", "0", "--dry-run"],
            )
            self.assertIn(proc.returncode, (0, 1))
            self.assertEqual(trace.read_bytes(), before)

            # decisions --fill-gaps --dry-run: log unchanged
            log = Path(tmp) / "decisions.jsonl"
            log.write_text('{"ts": 1, "jev_status": "fill"}\n', encoding="utf-8")
            before = log.read_bytes()
            proc = _run(
                "decisions.py",
                ["--file", str(log), "--fill-gaps", "--dry-run"],
            )
            self.assertIn(proc.returncode, (0, 1))
            self.assertEqual(log.read_bytes(), before)

    def test_compact_dry_run_no_spill_or_output_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "t.jsonl"
            transcript.write_text(
                '{"role": "user", "content": "hi"}\n'
                '{"role": "assistant", "content": "yo"}\n',
                encoding="utf-8",
            )
            before = transcript.read_bytes()
            proc = _run(
                "compact.py",
                [str(transcript), "--fake", "--history", "--dry-run"],
            )
            self.assertIn(proc.returncode, (0, 1))
            self.assertEqual(transcript.read_bytes(), before)
            # no spill artifacts created in the tmp dir
            stray = [p.name for p in Path(tmp).iterdir() if p.name != "t.jsonl"]
            self.assertEqual(stray, [], "compact --dry-run wrote: %s" % stray)


if __name__ == "__main__":
    unittest.main()

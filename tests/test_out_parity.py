"""--out payload parity: files land and unwritable paths fail cleanly.

Two conventions are pinned:
- `--out PATH` on report scripts writes the payload to PATH (stdout
  still prints it); the file parses in the script's format.
- `--out` into a path that cannot be created exits 1 with a
  `cannot write` stderr line — payload writes are real errors,
  unlike the env-report `--out` which is fail-open (rc 0).
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "skills" / "jev-consult" / "scripts"
SKILL_MD = ROOT / "skills" / "jev-consult" / "SKILL.md"
POLICY_JSON = ROOT / "skills" / "jev-consult" / "policy.json"
TRIGGER_CASES = ROOT / "tests" / "fixtures" / "jev-consult.trigger-cases.json"
EXAMPLE_REQ = ROOT / "skills" / "jev-consult" / "examples" / "jwt-auth.request.json"

# script -> argv prefix producing a single JSON object payload on --out
JSON_OUT = {
    "compare.py": ["--json"],
    "doctor.py": ["--json"],  # rc 1 when checks fail — still writes
    "inventory.py": ["--json"],
    "policy_lint.py": ["--json"],
    "question_lint.py": [str(EXAMPLE_REQ), "--json"],
    "skill_lint.py": [str(SKILL_MD), "--json"],
    "trigger_lint.py": ["--json"],
}

# decisions --out writes filtered ENTRIES as JSONL (per docs)
JSONL_OUT = {
    "decisions.py": ["--tail", "3"],
}

ALLOWED_RC = (0, 1)  # lints/doctor exit 1 when findings exist


def _run(name: str, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / name), *argv],
        capture_output=True,
        text=True,
        timeout=90,
        stdin=subprocess.DEVNULL,
    )


class OutParityTests(unittest.TestCase):
    def test_out_writes_parseable_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name, argv in sorted(JSON_OUT.items()):
                with self.subTest(script=name):
                    target = Path(tmp) / ("%s.out" % name)
                    proc = _run(name, *argv, "--out", str(target))
                    self.assertIn(
                        proc.returncode,
                        ALLOWED_RC,
                        "%s rc=%d: %s" % (name, proc.returncode, proc.stderr[:200]),
                    )
                    self.assertTrue(target.is_file(), "%s --out wrote nothing" % name)
                    payload = json.loads(target.read_text(encoding="utf-8"))
                    self.assertIsInstance(payload, dict, name)
                    self.assertTrue(payload, "%s payload empty" % name)

    def test_out_writes_jsonl_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name, argv in sorted(JSONL_OUT.items()):
                with self.subTest(script=name):
                    target = Path(tmp) / ("%s.jsonl" % name)
                    proc = _run(name, *argv, "--out", str(target))
                    self.assertEqual(
                        proc.returncode,
                        0,
                        "%s rc=%d: %s" % (name, proc.returncode, proc.stderr[:200]),
                    )
                    rows = [
                        json.loads(line)
                        for line in target.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    self.assertTrue(rows, "%s wrote empty file" % name)
                    for row in rows:
                        self.assertIsInstance(row, dict, name)

    def test_out_leaves_no_tmp_residue(self) -> None:
        """Atomic writes clean up: after --out succeeds no *.tmp sibling remains."""
        with tempfile.TemporaryDirectory() as tmp:
            for name, argv in sorted(JSON_OUT.items()):
                with self.subTest(script=name):
                    target = Path(tmp) / ("%s.out" % name)
                    proc = _run(name, *argv, "--out", str(target))
                    self.assertIn(proc.returncode, ALLOWED_RC, name)
                    self.assertTrue(target.is_file(), name)
                    leftovers = [p.name for p in Path(tmp).glob("*.tmp")]
                    self.assertEqual(
                        leftovers, [], "%s left tmp files: %s" % (name, leftovers)
                    )

    def test_out_and_jq_write_file_and_print_field(self) -> None:
        """--out PATH with --jq KEY must do BOTH: the file gets the full
        payload, stdout gets just the jq'd field (same convention as the
        env-report --out and as compare/trigger_eval/smoke already had)."""
        with tempfile.TemporaryDirectory() as tmp:
            cases = {
                "skill_lint.py": ([str(SKILL_MD)], "findings"),
                "policy_lint.py": ([str(POLICY_JSON)], "errors"),
                "question_lint.py": ([str(EXAMPLE_REQ)], "findings"),
                "trigger_lint.py": ([str(TRIGGER_CASES)], "errors"),
                "trigger_eval.py": (["--json"], "ok"),
                "compare.py": (["--json"], "rows"),
                "doctor.py": (["--json"], "ok"),
                "inventory.py": (["--task", "x", "--home", tmp, "--json"], "counts"),
                "smoke.py": (["--only", "self_test"], "ok"),
                "apply_fill.py": (["--status", "--cwd", tmp], "miss"),
                "catalog_fill.py": (["--status", "--cwd", tmp], "miss"),
                "peer_fill.py": (["--status", "--cwd", tmp], "miss"),
            }
            for name, (argv, key) in sorted(cases.items()):
                with self.subTest(script=name):
                    target = Path(tmp) / ("%s.jqout" % name)
                    proc = _run(name, *argv, "--jq", key, "--out", str(target))
                    self.assertIn(
                        proc.returncode,
                        ALLOWED_RC,
                        "%s rc=%d: %s" % (name, proc.returncode, proc.stderr[:300]),
                    )
                    self.assertTrue(
                        target.is_file(), "%s --out skipped by --jq" % name
                    )
                    payload = json.loads(target.read_text(encoding="utf-8"))
                    self.assertTrue(
                        proc.stdout.strip(), "%s printed no jq value" % name
                    )
                    # stdout must be the field value, not the whole payload
                    if isinstance(payload, dict) and key in payload:
                        self.assertEqual(
                            json.loads(proc.stdout), payload[key], name
                        )

    def test_trace_stats_out_and_jq(self) -> None:
        """trace stats --out is 'instead of stdout', but --jq still prints
        the field after the file write lands."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "stats.out"
            missing = str(Path(tmp) / "no-trace.json")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "trace.py"),
                    "--file",
                    missing,
                    "stats",
                    "--jq",
                    "exists",
                    "--out",
                    str(target),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[:300])
            self.assertTrue(target.is_file(), "stats --out skipped by --jq")
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(proc.stdout.strip(), "false")
            self.assertEqual(payload["exists"], False)

    def test_out_bad_path_errors_cleanly(self) -> None:
        missing_dir = "nonexistent-dir-xyz"  # relative, guaranteed absent
        probes = dict(JSON_OUT)
        probes.update(JSONL_OUT)
        for name, argv in sorted(probes.items()):
            with self.subTest(script=name):
                target = str(Path(missing_dir) / "x.out")
                proc = _run(name, *argv, "--out", target)
                self.assertEqual(
                    proc.returncode,
                    1,
                    "%s rc=%d expected clean write error" % (name, proc.returncode),
                )
                self.assertIn("cannot write", proc.stderr, name)
                self.assertNotIn("Traceback", proc.stderr, name)


if __name__ == "__main__":
    unittest.main()

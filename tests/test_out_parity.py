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

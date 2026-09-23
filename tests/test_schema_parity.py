from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "skills" / "jev-consult" / "scripts"

# Every script exposing a --schema flag (or trace.py's `schema` subcommand) must
# print the key contract: text rows "key: type (required|optional)" and, with
# --json, a {key: {"required": bool, "type": str}} object.
FLAG_SCHEMA = ("compare.py", "policy_lint.py", "skill_lint.py", "trigger_eval.py")
SUBCOMMAND_SCHEMA = {"trace.py": "schema"}


class SchemaParityTests(unittest.TestCase):
    def _schema_args(self, name: str) -> list[str]:
        if name in SUBCOMMAND_SCHEMA:
            return [SUBCOMMAND_SCHEMA[name]]
        return ["--schema"]

    def _run(self, name: str, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / name), *self._schema_args(name), *extra],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_schema_rc0_and_contract_shape(self) -> None:
        names = sorted(FLAG_SCHEMA) + sorted(SUBCOMMAND_SCHEMA)
        for name in names:
            with self.subTest(script=name):
                proc = self._run(name, "--json")
                self.assertEqual(
                    proc.returncode,
                    0,
                    "%s schema rc=%d: %s" % (name, proc.returncode, proc.stderr[:300]),
                )
                rows = json.loads(proc.stdout)
                self.assertIsInstance(rows, dict)
                self.assertTrue(rows, "%s schema rows empty" % name)
                for key, meta in rows.items():
                    self.assertIsInstance(key, str)
                    self.assertIn("required", meta, "%s %s" % (name, key))
                    self.assertIsInstance(meta["required"], bool)
                    self.assertIsInstance(meta.get("type"), str)

    def test_schema_text_marks_required(self) -> None:
        for name in sorted(FLAG_SCHEMA) + sorted(SUBCOMMAND_SCHEMA):
            with self.subTest(script=name):
                proc = self._run(name)
                self.assertEqual(proc.returncode, 0, proc.stderr[:200])
                self.assertIn("(required)", proc.stdout)
                for line in proc.stdout.splitlines():
                    if line.strip():
                        self.assertRegex(line, r"^[A-Za-z0-9_.-]+: .+ \((required|optional|recommended)\)$")


if __name__ == "__main__":
    unittest.main()

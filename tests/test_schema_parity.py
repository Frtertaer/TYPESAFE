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
FLAG_SCHEMA = ("apply_fill.py", "catalog_fill.py", "compact.py", "compare.py", "decisions.py", "doctor.py", "inventory.py", "jev.py", "peer_fill.py", "policy_lint.py", "progress.py", "question_lint.py", "skill_lint.py", "trigger_eval.py")
SUBCOMMAND_SCHEMA = {"trace.py": "schema"}
FILL_SCRIPTS = ("apply_fill.py", "catalog_fill.py", "peer_fill.py")


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

    def test_fill_outcomes_cover_emit_literals(self) -> None:
        """Every emit(\"word ...\") literal must appear in OUTCOMES."""
        import re

        for name in FILL_SCRIPTS:
            with self.subTest(script=name):
                source = (SCRIPTS_DIR / name).read_text(encoding="utf-8")
                emitted = set(re.findall(r'emit\("([a-z_]+)', source))
                if 'emit("%s' in source:
                    # %s-prefixed lines carry the dry-run tag first
                    emitted.add("dry")
                match = re.search(r"OUTCOMES = \(([^)]+)\)", source)
                self.assertIsNotNone(match, "%s lacks OUTCOMES" % name)
                declared = set(re.findall(r'"([a-z_]+)"', match.group(1)))
                self.assertEqual(emitted - declared, set(), name)

    def test_smoke_covers_every_schema_script(self) -> None:
        """smoke.py's SCHEMA_SCRIPTS map must list every contract script."""
        import re

        source = (SCRIPTS_DIR / "smoke.py").read_text(encoding="utf-8")
        match = re.search(r"SCHEMA_SCRIPTS = \{(.*?)\}", source, re.S)
        self.assertIsNotNone(match, "smoke.py lacks SCHEMA_SCRIPTS")
        declared = set(re.findall(r'"([a-z_]+\.py)":', match.group(1)))
        self.assertEqual(declared, set(FLAG_SCHEMA) | set(SUBCOMMAND_SCHEMA))
        for name, sub in SUBCOMMAND_SCHEMA.items():
            self.assertIn('"%s": ("%s",)' % (name, sub), match.group(1))

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

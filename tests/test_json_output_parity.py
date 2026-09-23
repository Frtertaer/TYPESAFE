"""Every pure --json surface emits a parseable JSON object on rc 0.

--json is the machine contract: it must stay parseable (for --jq and
downstream piping) and must not print usage text or exit non-zero on a
self-contained invocation. Stateful or stdin-driven scripts (trace,
progress, compact, smoke, apply_fill, install, inventory_hook,
skill_scanner) are excluded because their --json needs a file/stdin
payload to produce output.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "skills" / "jev-consult" / "scripts"

# script -> argv that must produce a parseable JSON object on stdout.
# --json is appended last except where the CLI requires flag-first ordering.
JSON_PROBES = {
    "catalog_fill.py": ["--json"],
    "compare.py": ["--json"],
    "decisions.py": ["--json"],
    "doctor.py": ["--json"],  # exits 1 when checks fail — JSON still parses
    "inventory.py": ["--json"],
    "jev.py": ["--json", "env"],
    "peer_fill.py": ["--json"],
    "policy_lint.py": ["--json"],
    "question_lint.py": [
        str(ROOT / "skills" / "jev-consult" / "examples" / "jwt-auth.request.json"),
        "--json",
    ],
    "skill_lint.py": [
        str(ROOT / "skills" / "jev-consult" / "SKILL.md"),
        "--json",
    ],
    "trigger_eval.py": ["--self-test", "--json"],
    "trigger_lint.py": ["--json"],
}


class JsonOutputParityTests(unittest.TestCase):
    def test_json_flags_emit_parseable_object(self) -> None:
        for name, argv in sorted(JSON_PROBES.items()):
            with self.subTest(script=name):
                proc = subprocess.run(
                    [sys.executable, str(SCRIPTS_DIR / name), *argv],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                ok_rc = (0, 1) if name == "doctor.py" else (0,)
                self.assertIn(
                    proc.returncode,
                    ok_rc,
                    "%s rc=%d: %s" % (name, proc.returncode, proc.stderr[:300]),
                )
                payload = json.loads(proc.stdout)
                self.assertIsInstance(payload, dict, "%s --json not an object" % name)
                self.assertTrue(payload, "%s --json empty object" % name)

    def test_json_still_valid_when_findings_exist(self) -> None:
        """--json stays parseable on the rc-1 findings path, not just clean."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bad_md = Path(tmp) / "bad.md"
            bad_md.write_text("# no frontmatter\njust text\n", encoding="utf-8")
            bad_json = Path(tmp) / "bad.json"
            bad_json.write_text('{"qid": "x"}\n', encoding="utf-8")
            probes = (
                ("skill_lint.py", [str(bad_md)]),
                ("policy_lint.py", [str(bad_json)]),
            )
            for name, argv in probes:
                with self.subTest(script=name):
                    proc = subprocess.run(
                        [sys.executable, str(SCRIPTS_DIR / name), *argv, "--json"],
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    self.assertEqual(proc.returncode, 1, "%s rc=%d" % (name, proc.returncode))
                    payload = json.loads(proc.stdout)
                    findings = payload.get("findings")
                    self.assertIsInstance(findings, list)
                    self.assertTrue(findings, "%s rc1 with empty findings" % name)

    def test_no_script_prints_usage_on_json(self) -> None:
        """'usage:' on stdout means argparse rejected the probe argv."""
        for name, argv in sorted(JSON_PROBES.items()):
            with self.subTest(script=name):
                proc = subprocess.run(
                    [sys.executable, str(SCRIPTS_DIR / name), *argv],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertNotIn("usage:", proc.stdout.lower(), name)


if __name__ == "__main__":
    unittest.main()

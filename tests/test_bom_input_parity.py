"""UTF-8 BOM and CRLF input tolerance across the pack's file consumers.

Windows editors (Notepad, PowerShell ISE) routinely write UTF-8 with a
BOM. Every lint that reads a user-supplied file must accept it —
`utf-8-sig` decodes plain UTF-8 identically and strips the BOM. These
probes write BOM'd + CRLF'd inputs and assert the file is *read* (any
semantic lint error is fine; a decode/parse error is the regression).
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


def _bom(text: str) -> bytes:
    return b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8")


class BomInputParityTests(unittest.TestCase):
    def _run(self, name: str, path: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / name), str(path)],
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )

    def test_json_lints_parse_bom_crlf_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("question_lint.py", "policy_lint.py", "trigger_lint.py"):
                f = Path(tmp) / ("bom-%s.json" % name)
                f.write_bytes(_bom('{"qid": "x", "triggers": []}\n'))
                with self.subTest(script=name):
                    proc = self._run(name, f)
                    blob = proc.stdout + proc.stderr
                    self.assertIn(proc.returncode, (0, 1), "%s rc=%d: %s" % (name, proc.returncode, blob[:200]))
                    self.assertNotIn("BOM", blob)
                    self.assertNotIn("cannot parse", blob.lower())
                    self.assertNotIn("cannot read", blob.lower())

    def test_skill_lint_parses_bom_crlf_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "SKILL.md"
            f.write_bytes(
                _bom("---\nname: x\ndescription: y\n---\n# body\n")
            )
            proc = self._run("skill_lint.py", f)
            blob = proc.stdout + proc.stderr
            self.assertIn(proc.returncode, (0, 1), blob[:200])
            self.assertNotIn("no frontmatter block", blob)

    def test_decisions_log_tolerates_bom(self) -> None:
        """A BOM'd decisions.jsonl --file still parses its entries."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "decisions", SCRIPTS_DIR / "decisions.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        entry = {"ts": 1, "jev_status": "fill"}
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            log.write_bytes(b"\xef\xbb\xbf" + (json.dumps(entry) + "\n").encode())
            entries, bad = mod.load_entries(log)
            self.assertEqual(bad, 0)
            self.assertEqual(entries, [entry])


if __name__ == "__main__":
    unittest.main()

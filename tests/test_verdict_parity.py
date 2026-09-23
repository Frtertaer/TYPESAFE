"""--verdict PATH one-shot contract across the pack.

Scripts that accept --verdict write a slim JSON verdict payload even
without --watch: at minimum a {"verdict": <word>} object. Scripts where
--verdict is watch-scoped only (compact, inventory_hook, jev ping, trace,
progress status, smoke) are excluded or probed under --watch — probed:
they write nothing one-shot.
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
SKILL_MD = ROOT / "skills" / "jev-consult" / "SKILL.md"
EXAMPLE_REQ = ROOT / "skills" / "jev-consult" / "examples" / "jwt-auth.request.json"


def _run(name: str, argv: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / name), *argv],
        capture_output=True,
        text=True,
        timeout=90,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
    )


class VerdictParityTests(unittest.TestCase):
    def test_one_shot_verdict_file_written(self) -> None:
        probes = (
            ("apply_fill.py", []),
            ("catalog_fill.py", ["--task", "x", "--list"]),
            ("compare.py", []),
            ("decisions.py", ["--tail", "3"]),
            ("doctor.py", []),
            ("inventory.py", []),
            ("peer_fill.py", []),
            ("policy_lint.py", []),
            ("question_lint.py", [str(EXAMPLE_REQ)]),
            ("skill_lint.py", [str(SKILL_MD)]),
            ("trigger_eval.py", []),
            ("trigger_lint.py", []),
        )
        with tempfile.TemporaryDirectory() as tmp:
            for name, argv in probes:
                with self.subTest(script=name):
                    target = Path(tmp) / ("v-%s.json" % name)
                    proc = _run(name, [*argv, "--verdict", str(target)], tmp)
                    # doctor legitimately reports rc 1 when checks fail;
                    # a usage error (2) or crash is the contract break.
                    self.assertIn(proc.returncode, (0, 1), "%s rc=%d" % (name, proc.returncode))
                    self.assertTrue(
                        target.is_file(), "%s wrote no verdict file" % name
                    )
                    payload = json.loads(target.read_text(encoding="utf-8"))
                    self.assertIsInstance(payload, dict)
                    verdict = payload.get("verdict")
                    self.assertIsInstance(verdict, str)
                    self.assertTrue(verdict, "%s verdict empty" % name)
                    self.assertNotIn("Traceback", proc.stderr)

    def test_compact_watch_verdict(self) -> None:
        """compact's --verdict is watch-scoped: one capped tick writes it."""
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "t.jsonl"
            transcript.write_text(
                '{"role": "user", "content": "hi"}\n'
                '{"role": "assistant", "content": "yo"}\n',
                encoding="utf-8",
            )
            target = Path(tmp) / "v-compact.json"
            proc = _run(
                "compact.py",
                [
                    str(transcript),
                    "--watch",
                    "0.05",
                    "--max-ticks",
                    "1",
                    "--fake",
                    "--history",
                    "--verdict",
                    str(target),
                ],
                tmp,
            )
            self.assertIn(proc.returncode, (0, 1), proc.stderr[:200])
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertIn(payload["verdict"], ("ok", "fallback"))


if __name__ == "__main__":
    unittest.main()

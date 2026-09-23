"""Non-ASCII argv values must not crash the pack.

On Windows, subprocess argv is already unicode — the risk is an
encode('ascii') or a logging path that dies on the value. These probes
pass unicode file names, --pick values, and --where text through real
subprocess invocations and require rc in {0,1,2} with no traceback.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "skills" / "jev-consult" / "scripts"

UNICODE_NAME = "ünïcode-.md"
UNICODE_PICK = "-αβγ"
UNICODE_TEXT = "Сообщение-ümlaut-"


def _run(name: str, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / name), *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


class UnicodeArgvParityTests(unittest.TestCase):
    def test_unicode_filename_lints_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / UNICODE_NAME
            md.write_text(
                "---\nname: x\ndescription: y\n---\nbody\n", encoding="utf-8"
            )
            proc = _run("skill_lint.py", str(md))
            self.assertIn(proc.returncode, (0, 1))
            self.assertNotIn("Traceback", proc.stderr + proc.stdout)

    def test_unicode_flag_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / "t.trace.json"
            _run("trace.py", "--file", str(trace), "init", "--plan", UNICODE_PICK)
            log = Path(tmp) / "decisions.jsonl"
            log.write_text(
                '{"ts": 1, "jev_status": "x", "prompt_head": "%s"}\n'
                % UNICODE_TEXT,
                encoding="utf-8",
            )
            probes = (
                (
                    "trace.py",
                    (
                        "--file",
                        str(trace),
                        "record",
                        "--pick",
                        UNICODE_PICK,
                        "--kind",
                        "next_move",
                    ),
                ),
                (
                    "decisions.py",
                    ("--file", str(log), "--tail", "5", "--where", UNICODE_TEXT),
                ),
                ("question_lint.py", ("--explain", UNICODE_TEXT)),
            )
            for name, argv in probes:
                with self.subTest(script=name):
                    proc = _run(name, *argv)
                    self.assertIn(
                        proc.returncode,
                        (0, 1, 2),
                        "%s rc=%d: %s" % (name, proc.returncode, proc.stderr[:200]),
                    )
                    self.assertNotIn("Traceback", proc.stderr + proc.stdout)
                    self.assertNotIn(
                        "UnicodeEncodeError", proc.stderr + proc.stdout
                    )


if __name__ == "__main__":
    unittest.main()

"""`--jq KEY` on an unknown key exits 2 with `bad --jq key` on stderr.

This contract is documented in SKILL.md for the payload emitters. Scripts
whose --jq is a per-entry field extractor (decisions, the *_lint tools,
compact) intentionally do not follow it and are not in the table.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"


def _run(script: str, argv: list, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script)] + argv,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=60,
    )


class JqContractTests(unittest.TestCase):
    def test_bad_jq_key_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            answers = tmp / "answers.json"
            answers.write_text(
                json.dumps(
                    {
                        "answers": {
                            "q": {
                                "type": "choice",
                                "choice": "a",
                                "confidence": 0.9,
                                "probabilities": {"a": 0.9, "b": 0.1},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            cases = [
                ("inventory.py", ["--jq", "nope.nope"]),
                ("inventory_hook.py", ["--env", "--jq", "nope"]),
                ("doctor.py", ["--jq", "nope"]),
                ("compare.py", ["--jq", "nope"]),
                ("smoke.py", ["--only", "policy", "--jq", "nope"]),
                ("peer_fill.py", ["--status", "--jq", "nope"]),
                (
                    "apply_fill.py",
                    ["--status", "--cwd", str(tmp), "--jq", "nope"],
                ),
                ("jev.py", ["decide", str(answers), "--jq", "nope"]),
                ("trigger_eval.py", ["--env", "--jq", "nope"]),
            ]
            problems = []
            for script, argv in cases:
                proc = _run(script, argv, tmp)
                if proc.returncode != 2 or "bad --jq key" not in proc.stderr:
                    problems.append(
                        "%s: rc=%d err=%r"
                        % (script, proc.returncode, proc.stderr[:100])
                    )
            self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

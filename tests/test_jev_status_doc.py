"""Every jev_status value a script can emit is documented in SKILL.md.

decisions.jsonl consumers filter on `jev_status`; an undocumented status
means a reader cannot interpret the log. Scans the pack for
`"status": "<tok>"` / `"jev_status": "<tok>"` literals and checks each
token appears in SKILL.md next to `jev_status`.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
SKILL_MD = ROOT / "skills" / "jev-consult" / "SKILL.md"

STATUS_LITERAL = re.compile(r'"(?:jev_)?status":\s*"([a-z_]+)"')
STATUS_ASSIGN = re.compile(r'\bstatus\s*=\s*"([a-z_]+)"')


class JevStatusDocTests(unittest.TestCase):
    def test_status_vocabulary_is_documented(self) -> None:
        doc = SKILL_MD.read_text(encoding="utf-8")
        vocab = set()
        for path in sorted(SCRIPTS.glob("*.py")):
            if path.name in {"skill_scanner.py", "run_trigger_evals.py"}:
                continue  # vendored
            src = path.read_text(encoding="utf-8")
            vocab.update(STATUS_LITERAL.findall(src))
            if path.name in {"inventory_hook.py", "inventory.py"}:
                vocab.update(STATUS_ASSIGN.findall(src))
        # non-status literals that share the key name (unrelated payloads)
        vocab -= {"empty_result", "ok", "pass", "fail", "stable", "changed",
                  "proceed", "unknown", "warn"}
        undocumented = sorted(
            s for s in vocab if "jev_status=%s" % s not in doc
            and "`%s`" % s not in doc
        )
        self.assertEqual(undocumented, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

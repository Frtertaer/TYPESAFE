"""Missing-value parity: a valued flag with no value must exit 2.

Manual parsers print `--flag needs a VALUE` to stderr; argparse prints
usage and exits 2. Either way a dangling flag must never silently run
the default action. Hooks are exempt-by-design: they fail open (rc 0,
`{}`) so a malformed args line can never fail the harness.
"""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "skills" / "jev-consult" / "scripts"

# script -> flags that take a value; each is probed as the LAST argv token.
MISSING_VALUE_RC2 = {
    "apply_fill.py": ["--task", "--harness", "--pick", "--policy"],
    "catalog_fill.py": ["--task", "--harness", "--pick", "--policy"],
    "peer_fill.py": ["--task", "--harness", "--pick", "--policy"],
    "compare.py": ["--jq", "--out", "--watch"],
    "compact.py": ["-o", "--trace", "--goal"],
    "decisions.py": ["--file", "--tail", "--days", "--jq", "--out"],
    "doctor.py": ["--agents", "--jq", "--out"],
    "inventory.py": ["--ttl", "--limit", "--out"],
    "jev.py": ["--policy"],
    "progress.py": ["--repo", "--db", "--policy"],
    "policy_lint.py": [
        "--severity",
        "--explain",
        "--out",
        "--diff",
        "--watch",
        "--jq",
        "--max-ticks",
        "--watch-max",
        "--verdict",
        "--baseline",
        "--baseline-write",
    ],
    "question_lint.py": [
        "--severity",
        "--explain",
        "--out",
        "--watch",
        "--jq",
        "--max-ticks",
        "--watch-max",
        "--verdict",
        "--baseline",
        "--baseline-write",
    ],
    "skill_lint.py": [
        "--severity",
        "--watch",
        "--watch-max",
        "--max-ticks",
        "--jq",
        "--out",
        "--verdict",
        "--explain",
        "--baseline",
        "--baseline-write",
    ],
    "skill_scanner.py": ["--top", "--task", "--explain"],
    "smoke.py": ["--only", "--repeat", "--jobs", "--timeout", "--baseline", "--baseline-write"],
    "trace.py": ["--file"],
    "trigger_eval.py": [
        "--cases",
        "--jq",
        "--out",
        "--baseline",
        "--baseline-write",
    ],
    "trigger_lint.py": [
        "--severity",
        "--explain",
        "--out",
        "--policy",
        "--watch",
        "--jq",
        "--max-ticks",
        "--watch-max",
        "--verdict",
        "--baseline",
        "--baseline-write",
    ],
}

# Hooks must keep working (rc 0, empty object) even when a valued flag
# dangles — fail-open beats usage correctness inside a harness.
MISSING_VALUE_HOOK_RC0 = {
    "compact_hook.py": ["--file", "--out", "--verdict"],
    "inventory_hook.py": ["--file", "--jq", "--out"],
}


def _run(name: str, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / name), *argv],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


class MissingValueParityTests(unittest.TestCase):
    def test_dangling_valued_flag_exits_2(self) -> None:
        for name, flags in sorted(MISSING_VALUE_RC2.items()):
            for flag in flags:
                with self.subTest(script=name, flag=flag):
                    proc = _run(name, flag)
                    self.assertEqual(
                        proc.returncode,
                        2,
                        "%s %s rc=%d: %s%s"
                        % (name, flag, proc.returncode, proc.stdout[:150], proc.stderr[:150]),
                    )
                    self.assertTrue(
                        proc.stderr.strip() or proc.stdout.strip(),
                        "%s %s produced no message" % (name, flag),
                    )

    def test_hooks_fail_open_on_dangling_flag(self) -> None:
        import json

        for name, flags in sorted(MISSING_VALUE_HOOK_RC0.items()):
            for flag in flags:
                with self.subTest(script=name, flag=flag):
                    proc = _run(name, flag)
                    self.assertEqual(
                        proc.returncode,
                        0,
                        "%s %s rc=%d: %s" % (name, flag, proc.returncode, proc.stderr[:200]),
                    )
                    self.assertNotIn("Traceback", proc.stderr, name)
                    self.assertEqual(json.loads(proc.stdout.strip() or "{}"), {})


if __name__ == "__main__":
    unittest.main()

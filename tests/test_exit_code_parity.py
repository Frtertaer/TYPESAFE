"""Exit-code contract pinned across the pack.

- Unknown flags on CLI tools exit 2 (usage error) — argparse or manual.
- Hooks (compact_hook, inventory_hook) exit 0 on the same garbage —
  a hook must never fail the harness it is installed into.
- A bad --jq key exits 2 on every --env/--watch surface (covered in
  test_env_report_parity; here only the unknown-flag axis).
"""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "skills" / "jev-consult" / "scripts"

BAD_FLAG = "--definitely-not-a-real-flag"

USAGE_RC = {
    "apply_fill.py",
    "catalog_fill.py",
    "catalog_inventory_hook.py",
    "compare.py",
    "compact.py",
    "decisions.py",
    "decisions_hook.py",
    "doctor.py",
    "inventory.py",
    "jev.py",
    "peer_fill.py",
    "policy_lint.py",
    "progress.py",
    "question_lint.py",
    "skill_lint.py",
    "skill_scanner.py",
    "smoke.py",
    "trace.py",
    "trigger_eval.py",
    "trigger_lint.py",
}

# Hooks absorb unknown args — failing the harness on a flag typo would be
# worse than ignoring it (fail-open rule).
FAIL_OPEN_RC0 = {
    "compact_hook.py",
    "inventory_hook.py",
}

# Library modules, not CLIs.
NON_CLI = {"progress_core.py"}


def _run(name: str, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / name), *argv],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


class ExitCodeParityTests(unittest.TestCase):
    def test_unknown_flag_exits_2_on_cli_tools(self) -> None:
        for name in sorted(USAGE_RC):
            with self.subTest(script=name):
                proc = _run(name, BAD_FLAG)
                self.assertEqual(
                    proc.returncode,
                    2,
                    "%s rc=%d: %s%s"
                    % (name, proc.returncode, proc.stdout[:200], proc.stderr[:200]),
                )

    def test_hooks_fail_open_on_unknown_flag(self) -> None:
        for name in sorted(FAIL_OPEN_RC0):
            with self.subTest(script=name):
                proc = _run(name, BAD_FLAG)
                self.assertEqual(
                    proc.returncode,
                    0,
                    "%s rc=%d: %s" % (name, proc.returncode, proc.stderr[:200]),
                )
                self.assertNotIn("Traceback", proc.stderr, name)

    def test_every_script_classified(self) -> None:
        covered = USAGE_RC | FAIL_OPEN_RC0
        present = {
            f.name
            for f in SCRIPTS_DIR.glob("*.py")
            if not f.name.startswith("_")
        } - NON_CLI
        self.assertEqual(
            present - covered,
            set(),
            "scripts not classified for exit-code parity: %s"
            % sorted(present - covered),
        )


if __name__ == "__main__":
    unittest.main()

"""Pin: every pack script that ships a self-test surface must run it green.

`--self-test` (or the `self-test` subcommand) is the pack's offline health
probe — a script whose self-test regresses silently would ship broken. This
suite invokes each self-test in a subprocess and requires exit 0 plus the
script's success marker on stdout.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "jev-consult" / "scripts"

# script -> (argv tail, success marker substring expected on stdout)
SELF_TESTS = {
    "apply_fill.py": (["--self-test"], "self-test: ok"),
    "catalog_fill.py": (["--self-test"], "self-test: ok"),
    "compact.py": (["--self-test"], "self-test: ok"),
    "compact_hook.py": (["--self-test"], "self-test:"),
    "compare.py": (["--self-test"], "self-test: ok"),
    "decisions.py": (["--self-test"], "self-test: ok"),
    "doctor.py": (["--self-test"], "self-test: ok"),
    "inventory.py": (["--self-test"], "self-test: ok"),
    "inventory_hook.py": (["--self-test"], "self-test:"),
    "jev.py": (["self-test"], ""),
    "peer_fill.py": (["--self-test"], "self-test: ok"),
    "policy_lint.py": (["--self-test"], ""),
    "progress.py": (["self-test"], '"self_test"'),
    "question_lint.py": (["--self-test"], "self-test: ok"),
    "skill_lint.py": (["--self-test"], "self-test: ok"),
    "skill_scanner.py": (["--self-test"], "self-test: ok"),
    "smoke.py": (["--self-test"], "self-test: ok"),
    "trace.py": (["self-test"], '"self_test"'),
    "trigger_eval.py": (["--self-test"], "self-test: ok"),
    "trigger_lint.py": (["--self-test"], "self-test: ok"),
}


class SelfTestParityTests(unittest.TestCase):
    def test_parity_map_covers_every_selftest_script(self) -> None:
        discovered = set()
        for path in sorted(SCRIPTS.glob("*.py")):
            if path.name.startswith("_"):
                continue
            src = path.read_text(encoding="utf-8")
            if '"--self-test"' in src or "self-test" in src:
                discovered.add(path.name)
        missing = discovered - set(SELF_TESTS)
        self.assertEqual(
            missing,
            set(),
            "scripts exposing a self-test but missing from SELF_TESTS: %s"
            % sorted(missing),
        )

    def test_every_selftest_exits_zero(self) -> None:
        for script, (argv_tail, marker) in sorted(SELF_TESTS.items()):
            with self.subTest(script=script):
                r = subprocess.run(
                    [sys.executable, str(SCRIPTS / script)] + argv_tail,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=str(REPO),
                )
                self.assertEqual(
                    r.returncode,
                    0,
                    "%s self-test rc=%d stderr=%s"
                    % (script, r.returncode, r.stderr.strip()[:200]),
                )
                if marker:
                    self.assertIn(
                        marker,
                        r.stdout,
                        "%s self-test missing marker %r: %s"
                        % (script, marker, r.stdout.strip()[:200]),
                    )

    def test_selftest_rc1_on_failure_marker_absent(self) -> None:
        # spot check: jev.py self-test emits a JSON verdict we can parse
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "jev.py"), "self-test", "--json"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO),
        )
        self.assertEqual(r.returncode, 0)
        payload = json.loads(r.stdout)
        self.assertEqual(payload.get("self_test"), "ok")


if __name__ == "__main__":
    unittest.main()

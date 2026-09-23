"""Every pack script's --version prints `jev-consult (policy vN)`.

N must equal the `version` field in skills/jev-consult/policy.json.
Vendored files (skill_scanner.py, run_trigger_evals.py) are exempt.
"""

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
POLICY = ROOT / "skills" / "jev-consult" / "policy.json"
VENDORED = {"skill_scanner.py", "run_trigger_evals.py"}
VERSION_RE = re.compile(r"^jev-consult \(policy v(\d+)\)$")


def _pack_scripts() -> list[Path]:
    return [
        p
        for p in sorted(SCRIPTS.glob("*.py"))
        if p.name not in VENDORED and p.name != "_watch.py"
    ]


class VersionParityTests(unittest.TestCase):
    def test_every_script_supports_version(self) -> None:
        problems = []
        for path in _pack_scripts():
            src = path.read_text(encoding="utf-8")
            if "maybe_version" not in src and "args.version" not in src:
                problems.append(path.name)
        self.assertEqual(problems, [])

    def test_version_output_matches_policy(self) -> None:
        policy_version = json.loads(POLICY.read_text(encoding="utf-8"))["version"]
        problems = []
        for path in _pack_scripts():
            proc = subprocess.run(
                [sys.executable, str(path), "--version"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            line = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
            match = VERSION_RE.match(line)
            if proc.returncode != 0 or not match:
                problems.append("%s: rc=%d out=%r" % (path.name, proc.returncode, line))
                continue
            if int(match.group(1)) != policy_version:
                problems.append(
                    "%s: v%s != policy v%s"
                    % (path.name, match.group(1), policy_version)
                )
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

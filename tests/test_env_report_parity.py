#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""--env report contract: every env-capable script prints a resolved-config
JSON object covering its pinned key subset, supports --env --jq KEY, and
rejects unknown jq keys with rc 2. Also guards that no report leaks a
secret-shaped key or the live TYPESAFE_API_KEY value."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

# Scripts that must accept --env, and the required keys each report exposes.
# Additive: new keys are fine, missing pinned keys fail.
ENV_SCRIPTS = {
    "inventory_hook.py": {
        "budget_seconds",
        "dedupe_ttl_seconds",
        "limit",
        "miss_present",
        "policy",
        "sidecar_present",
        "ttl_seconds",
        "watch_max",
        "watch_quiet",
        "watch_secs",
    },
    "trigger_eval.py": {
        "cases",
        "cases_exists",
        "margin",
        "min_coverage",
        "min_covers",
        "scorer",
        "scorer_exists",
        "skill",
        "watch_max",
        "watch_quiet",
        "watch_secs",
    },
    "doctor.py": {"count", "env"},
    "compact_hook.py": {
        "live_fat",
        "live_head",
        "live_tail",
        "policy",
        "spill_dir",
        "spill_disabled",
        "spill_max_bytes",
        "spill_max_files",
    },
    "peer_fill.py": {"fill_timeout_seconds", "policy", "watch_max", "watch_quiet", "watch_secs"},
    "apply_fill.py": {"fill_timeout_seconds", "policy", "watch_max", "watch_quiet", "watch_secs"},
}

SECRETISH = ("api_key", "token", "secret", "password")


def _run(script, argv):
    env = dict(os.environ)
    env["TYPESAFE_API_KEY"] = "typesafe-test-key-do-not-leak"
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *argv],
        input="",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


class EnvReportParityTests(unittest.TestCase):
    def test_every_script_reports_json_object_with_pinned_keys(self) -> None:
        for name, required in ENV_SCRIPTS.items():
            with self.subTest(script=name):
                proc = _run(name, ["--env"])
                self.assertEqual(proc.returncode, 0, proc.stderr)
                report = json.loads(proc.stdout)
                self.assertIsInstance(report, dict)
                missing = required - set(report)
                self.assertFalse(
                    missing, "%s --env missing pinned keys %s" % (name, sorted(missing))
                )

    def test_no_secretish_key_or_key_value_leak(self) -> None:
        for name in ENV_SCRIPTS:
            with self.subTest(script=name):
                proc = _run(name, ["--env"])
                self.assertEqual(proc.returncode, 0, proc.stderr)
                report = json.loads(proc.stdout)
                for key, value in report.items():
                    lowered = key.lower()
                    for marker in SECRETISH:
                        self.assertNotIn(
                            marker, lowered, "%s report exposes key %r" % (name, key)
                        )
                    if isinstance(value, str):
                        self.assertNotIn(
                            "typesafe-test-key-do-not-leak",
                            value,
                            "%s leaked the API key via %r" % (name, key),
                        )

    def test_jq_field_and_unknown_key_rc2(self) -> None:
        for name, required in ENV_SCRIPTS.items():
            first = sorted(required)[0]
            with self.subTest(script=name):
                proc = _run(name, ["--env", "--jq", first])
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                json.loads(proc.stdout.strip())
                proc = _run(name, ["--env", "--jq", "definitely_not_a_key"])
                self.assertEqual(proc.returncode, 2, name + " bad --jq key must exit 2")
                self.assertIn("has:", proc.stderr)

    def test_scripts_without_env_flag_fail_cleanly(self) -> None:
        for path in sorted(SCRIPTS.glob("*.py")):
            if path.name in ENV_SCRIPTS or path.name in ("smoke.py", "_watch.py"):
                continue
            with self.subTest(script=path.name):
                proc = _run(path.name, ["--env"])
                self.assertNotEqual(proc.returncode, 0, path.name + " unexpectedly took --env")


if __name__ == "__main__":
    unittest.main()

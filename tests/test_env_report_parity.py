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
LINT_ENV_KEYS = {
    "files",
    "quiet",
    "severity",
    "strict",
    "watch_max",
    "watch_quiet",
    "watch_secs",
}
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
    "doctor.py": {"count", "env", "policy", "watch_max", "watch_quiet", "watch_secs"},
    "inventory.py": {"limit", "log", "policy", "task", "watch_max", "watch_quiet", "watch_secs"},
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
    "decisions.py": {"count", "env", "exists", "file", "source"},
    "install.py": {
        "agents",
        "existing",
        "hermes_home",
        "home",
        "key_set",
        "policy",
        "targets",
    },
    "catalog_fill.py": {
        "ask",
        "catalog_cache_seconds",
        "fill_timeout_seconds",
        "miss",
        "policy",
        "watch_max",
        "watch_quiet",
        "watch_secs",
    },
    "compact.py": {
        "keep_first",
        "keep_threshold",
        "min_messages",
        "min_reduction",
        "policy",
        "preserve_recent",
        "spill_dir",
        "spill_disabled",
        "spill_max_bytes",
        "spill_max_files",
        "truncate_head_chars",
        "watch_max",
        "watch_quiet",
        "watch_secs",
    },
    "compare.py": {
        "cases",
        "cases_exists",
        "live",
        "only",
        "policy",
        "strict",
        "watch_max",
        "watch_quiet",
        "watch_secs",
    },
    "policy_lint.py": {
        "files",
        "policy",
        "quiet",
        "severity",
        "strict",
        "watch_max",
        "watch_quiet",
        "watch_secs",
    },
    "skill_lint.py": LINT_ENV_KEYS,
    "question_lint.py": LINT_ENV_KEYS,
    "trigger_lint.py": LINT_ENV_KEYS | {"policy"},
    "trace.py": {
        "exists",
        "file",
        "fill_timeout_seconds",
        "plan_set",
        "policy",
        "watch_max",
        "watch_quiet",
        "watch_secs",
    },
    "progress.py": {
        "db",
        "db_exists",
        "policy",
        "repo",
        "watch_max",
        "watch_quiet",
        "watch_secs",
    },
    "smoke.py": {
        "jobs",
        "only",
        "policy",
        "repeat",
        "steps",
        "timeout",
        "watch_max",
        "watch_quiet",
        "watch_secs",
    },
}

# Subcommand CLIs expose `env` instead of a --env flag; jev.py keeps its own
# dedicated tests (its report names api_key_set, which trips the leak guard).
ENV_SUBCOMMAND = {"trace.py", "progress.py"}

# Env-capable scripts living outside the pack scripts dir.
SCRIPT_PATHS = {"install.py": ROOT / "scripts" / "install.py"}


def _script_path(name):
    return SCRIPT_PATHS.get(name, SCRIPTS / name)


def _env_argv(name, *rest):
    base = ["env"] if name in ENV_SUBCOMMAND else ["--env"]
    return base + [str(a) for a in rest]

SECRETISH = ("api_key", "token", "secret", "password")


def _run(script, argv, extra_env=None):
    env = dict(os.environ)
    env["TYPESAFE_API_KEY"] = "typesafe-test-key-do-not-leak"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(_script_path(script)), *argv],
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
                proc = _run(name, _env_argv(name))
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
                proc = _run(name, _env_argv(name))
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
                proc = _run(name, _env_argv(name, "--jq", first))
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                json.loads(proc.stdout.strip())
                proc = _run(name, _env_argv(name, "--jq", "definitely_not_a_key"))
                self.assertEqual(proc.returncode, 2, name + " bad --jq key must exit 2")
                self.assertIn("has:", proc.stderr)

    def test_help_names_report_fields(self) -> None:
        """--env help must name at least one report field (self-documenting)."""
        for name, required in ENV_SCRIPTS.items():
            with self.subTest(script=name):
                proc = _run(name, _env_argv(name, "--help"))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                help_text = proc.stdout.lower()
                hits = [k for k in required if k.replace("_", "-") in help_text or k in help_text]
                self.assertTrue(
                    hits, "%s --help names no env report field" % name
                )

    def test_env_jq_prints_one_field_and_rejects_unknown(self) -> None:
        for name, keys in sorted(ENV_SCRIPTS.items()):
            field = sorted(keys)[0]
            with self.subTest(script=name, field=field):
                proc = _run(name, _env_argv(name, "--jq", field))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertTrue(proc.stdout.strip(), "empty --jq output")
            with self.subTest(script=name, field="bogus"):
                proc = _run(name, _env_argv(name, "--jq", "no_such_field_xyz"))
                self.assertEqual(proc.returncode, 2)
                self.assertIn(
                    "has:", proc.stderr, "--jq bad key must list valid fields"
                )

    def test_env_out_writes_report_file(self) -> None:
        import tempfile

        for name in ENV_SCRIPTS:
            with self.subTest(script=name), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "env.json"
                proc = _run(name, _env_argv(name, "--out", str(target)))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                saved = json.loads(target.read_text(encoding="utf-8"))
                self.assertIsInstance(saved, dict)
                self.assertTrue(ENV_SCRIPTS[name] <= set(saved))

    def test_env_overrides_land_in_report(self) -> None:
        """A JEV_* env override must show up in the script's --env report —
        guards knobs that exist but are silently not read."""
        probes = [
            ("inventory.py", {"JEV_LIMIT": "5"}, "limit", 5),
            ("inventory.py", {"JEV_INV_WATCH_MAX": "9"}, "watch_max", 9),
            (
                "inventory_hook.py",
                {"JEV_HOOK_LIMIT": "4"},
                "limit",
                4,
            ),
            (
                "inventory_hook.py",
                {"JEV_HOOK_TTL": "33"},
                "ttl_seconds",
                33.0,
            ),
            ("smoke.py", {"JEV_SMOKE_JOBS": "7"}, "jobs", 7),
            (
                "smoke.py",
                {"JEV_SMOKE_WATCH_QUIET": "1"},
                "watch_quiet",
                True,
            ),
            (
                "compare.py",
                {"JEV_COMPARE_ONLY": "a,b"},
                "only",
                "a,b",
            ),
            (
                "compact.py",
                {"JEV_KEEP_THRESHOLD": "0.5"},
                "keep_threshold",
                0.5,
            ),
            (
                "trace.py",
                {"JEV_FILL_TIMEOUT": "12"},
                "fill_timeout_seconds",
                12.0,
            ),
        ]
        for name, env, field, want in probes:
            with self.subTest(script=name, field=field):
                proc = _run(name, _env_argv(name, "--jq", field), extra_env=env)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(
                    json.loads(proc.stdout.strip()),
                    want,
                    "%s %s env override not honored" % (name, field),
                )

    def test_env_keys_sorted(self) -> None:
        """--env output keys are sorted — deterministic diffs in reviews."""
        for name in ENV_SCRIPTS:
            with self.subTest(script=name):
                proc = _run(name, _env_argv(name))
                payload = json.loads(proc.stdout)
                self.assertEqual(
                    list(payload), sorted(payload), "%s keys unsorted" % name
                )

    def test_env_out_file_matches_stdout(self) -> None:
        """--env --out PATH writes exactly the stdout report bytes."""
        import tempfile

        for name in ENV_SCRIPTS:
            with self.subTest(script=name):
                with tempfile.TemporaryDirectory() as tmp:
                    target = Path(tmp) / "env.json"
                    proc = _run(name, _env_argv(name, "--out", str(target)))
                    self.assertEqual(
                        proc.returncode, 0, "%s rc=%d" % (name, proc.returncode)
                    )
                    self.assertEqual(
                        target.read_text(encoding="utf-8"),
                        proc.stdout,
                        "%s --out file != stdout" % name,
                    )

    def test_env_jq_digs_nested_fields(self) -> None:
        """--env --jq honors dotted digs into nested objects and list indexes."""
        proc = _run("install.py", ["--env", "--jq", "targets.hermes.skills"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        skills = json.loads(proc.stdout.strip())
        self.assertIsInstance(skills, list)
        self.assertTrue(skills, "install.py targets.hermes.skills empty")

        proc = _run("install.py", ["--env", "--jq", "targets.hermes.skills.0"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsInstance(json.loads(proc.stdout.strip()), str)

        for name, env, field, want in (
            (
                "doctor.py",
                {"JEV_DOCTOR_WATCH_MAX": "3"},
                "env.JEV_DOCTOR_WATCH_MAX",
                "3",
            ),
            (
                "decisions.py",
                {"JEV_DECISIONS_TAIL": "5"},
                "env.JEV_DECISIONS_TAIL",
                "5",
            ),
        ):
            with self.subTest(script=name, field=field):
                proc = _run(name, _env_argv(name, "--jq", field), extra_env=env)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(json.loads(proc.stdout.strip()), want)

        # a miss inside a nested map still exits 2
        proc = _run(
            "doctor.py", ["--env", "--jq", "env.NO_SUCH_ENV_KEY_XYZ"]
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("has:", proc.stderr)

    def test_env_out_write_failure_is_fail_open(self) -> None:
        """--env --out into a missing dir still prints the report and exits 0."""
        import tempfile

        for name in ENV_SCRIPTS:
            with self.subTest(script=name), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "no-such-dir" / "env.json"
                proc = _run(name, _env_argv(name, "--out", str(target)))
                self.assertEqual(
                    proc.returncode,
                    0,
                    "%s --env --out bad path must stay rc 0: %s" % (name, proc.stderr),
                )
                self.assertNotIn("Traceback", proc.stderr)
                self.assertFalse(target.exists())
                report = json.loads(proc.stdout)
                self.assertTrue(ENV_SCRIPTS[name] <= set(report))

    def test_scripts_without_env_flag_fail_cleanly(self) -> None:
        for path in sorted(SCRIPTS.glob("*.py")):
            if path.name in ENV_SCRIPTS or path.name == "_watch.py":
                continue
            with self.subTest(script=path.name):
                proc = _run(path.name, ["--env"])
                self.assertNotEqual(proc.returncode, 0, path.name + " unexpectedly took --env")


if __name__ == "__main__":
    unittest.main()

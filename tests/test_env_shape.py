#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pin every script's --env (or env-subcommand) report key set.

New env knobs must land deliberately: adding a key here means the
report changed. Note: decisions --env nests set JEV_DECISIONS_* vars
under the `env` key (the others report flat keys).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

WATCH_KEYS = {"watch_max", "watch_quiet", "watch_secs"}

EXPECTED = {
    "decisions": {"count", "env", "exists", "file",
                  "source"} | WATCH_KEYS,
    "compact": {"keep_first", "keep_threshold", "min_messages",
                "min_reduction", "policy", "preserve_recent", "spill_dir",
                "spill_disabled", "spill_max_bytes", "spill_max_files",
                "truncate_head_chars"} | WATCH_KEYS,
    "compare": {"cases", "cases_exists", "live", "only", "policy",
                "strict"} | WATCH_KEYS,
    "doctor": {"count", "env", "policy"} | WATCH_KEYS,
    "inventory": {"limit", "log", "policy", "task"} | WATCH_KEYS,
    "policy_lint": {"files", "policy", "quiet", "severity",
                    "strict"} | WATCH_KEYS,
    "question_lint": {"files", "quiet", "severity", "strict"} | WATCH_KEYS,
    "skill_lint": {"files", "quiet", "severity", "strict"} | WATCH_KEYS,
    "trigger_lint": {"files", "policy", "quiet", "severity",
                     "strict"} | WATCH_KEYS,
    "smoke": {"jobs", "only", "policy", "repeat", "steps",
              "timeout"} | WATCH_KEYS,
    "compact_hook": {"live_fat", "live_head", "live_tail", "policy",
                     "spill_dir", "spill_disabled", "spill_max_bytes",
                     "spill_max_files"},
    "inventory_hook": {"budget_seconds", "dedupe_ttl_seconds", "events",
                       "jev_hook_cwd", "jev_hook_debug",
                       "jev_hook_debug_file", "jev_hook_event",
                       "jev_hook_events", "jev_hook_harness",
                       "jev_hook_nomiss", "jev_hook_nosidecar",
                       "jev_hook_off", "jev_hook_prompt",
                       "jev_hook_skip_events", "jev_hook_winner",
                       "jev_retries", "jev_timeout_seconds", "limit",
                       "max_age_seconds", "max_payload_bytes",
                       "max_prompt_chars", "miss_present", "note_limit",
                       "policy", "sidecar_present", "ttl_seconds",
                       "watch_dedupe"} | WATCH_KEYS,
    "apply_fill": {"fill_timeout_seconds", "policy"} | WATCH_KEYS,
    "catalog_fill": {"ask", "catalog_cache_seconds",
                     "fill_timeout_seconds", "miss", "policy"} | WATCH_KEYS,
    "peer_fill": {"fill_timeout_seconds", "policy"} | WATCH_KEYS,
}

# env as a subcommand rather than a flag.
ENV_SUBCOMMAND = {
    "trace": (["--file"], "trace.json", {"exists", "file",
             "fill_timeout_seconds", "plan_set", "policy"} | WATCH_KEYS),
    "jev": ([], None, {"api_key_set", "policy",
            "timeout_seconds"} | WATCH_KEYS),
    "progress": (["--repo"], "repo", {"db", "db_exists", "policy",
                 "repo"} | WATCH_KEYS),
}


def env_report(script: str, argv_prefix: list, argv_suffix: list, tmp: str) -> dict:
    env = dict(os.environ)
    env["JEV_CONSULT_LOG"] = "0"
    env.setdefault("TYPESAFE_API_KEY", "test-key")  # jev env needs presence only
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / script)] + argv_prefix + argv_suffix,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp,
        env=env,
    )
    if proc.returncode != 0:
        raise AssertionError("%s --env rc=%s: %s" % (script, proc.returncode, proc.stderr[:300]))
    return json.loads(proc.stdout)


class EnvShapeTests(unittest.TestCase):
    def test_flag_env_key_sets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name, keys in sorted(EXPECTED.items()):
                with self.subTest(script=name):
                    report = env_report(name + ".py", [], ["--env"], tmp)
                    self.assertEqual(set(report.keys()), keys)

    def test_subcommand_env_key_sets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name, (prefix, arg, keys) in sorted(ENV_SUBCOMMAND.items()):
                with self.subTest(script=name):
                    argv_prefix = list(prefix)
                    if arg:
                        argv_prefix.append(str(Path(tmp) / arg) if arg != "repo" else tmp)
                        if name == "progress":
                            argv_prefix += ["--db", str(Path(tmp) / "p.db")]
                    report = env_report(name + ".py", argv_prefix, ["env"], tmp)
                    self.assertEqual(set(report.keys()), keys)


if __name__ == "__main__":
    unittest.main()

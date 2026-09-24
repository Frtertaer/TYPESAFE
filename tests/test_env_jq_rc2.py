#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""`--env --jq BAD` must exit 2 on every env emitter (flag or
subcommand form) — an unknown key is a usage error, not an empty emit.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

FLAG_ENV = [
    "decisions", "compact", "compare", "doctor", "inventory",
    "policy_lint", "question_lint", "skill_lint", "trigger_lint",
    "smoke", "compact_hook", "inventory_hook", "apply_fill",
    "catalog_fill", "peer_fill",
]
SUB_ENV = {
    "trace": ["--file", "{tmp}/t.json"],
    "jev": [],
    "progress": ["--repo", "{tmp}", "--db", "{tmp}/p.db"],
}


def run(argv: list, cwd: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["JEV_CONSULT_LOG"] = "0"
    env.setdefault("TYPESAFE_API_KEY", "test-key")
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=60, cwd=cwd, env=env
    )


class EnvJqRc2Tests(unittest.TestCase):
    def test_flag_env_bad_jq_rc2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in FLAG_ENV:
                with self.subTest(script=name):
                    proc = run(
                        [sys.executable, str(SCRIPTS / (name + ".py")),
                         "--env", "--jq", "nonexistent_key_xyz"],
                        tmp,
                    )
                    self.assertEqual(proc.returncode, 2, proc.stderr[:200])
                    self.assertTrue(proc.stderr.strip(),
                                    "rc2 must explain the bad key on stderr")

    def test_subcommand_env_bad_jq_rc2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name, prefix in sorted(SUB_ENV.items()):
                with self.subTest(script=name):
                    args = [p.format(tmp=tmp) for p in prefix]
                    proc = run(
                        [sys.executable, str(SCRIPTS / (name + ".py")),
                         *args, "env", "--jq", "nonexistent_key_xyz"],
                        tmp,
                    )
                    self.assertEqual(proc.returncode, 2, proc.stderr[:200])
                    self.assertTrue(proc.stderr.strip())


if __name__ == "__main__":
    unittest.main()

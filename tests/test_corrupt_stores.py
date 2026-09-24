#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Consolidated no-traceback sweep: every on-disk store may be corrupt —
readers must tolerate it (fail-open for hooks, clean error for CLIs)
and NEVER leak a Python traceback to stderr."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
GARBAGE = "{oops not json\n"


def run(script: str, argv: list[str], tmp: str, env_extra: dict | None = None,
        stdin: str | None = None):
    env = dict(os.environ)
    env["JEV_CONSULT_LOG"] = "0"
    env.setdefault("TYPESAFE_API_KEY", "test-key")
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script)] + argv,
        capture_output=True, text=True, timeout=120, cwd=tmp, env=env,
        input=stdin,
    )


class CorruptStoreTests(unittest.TestCase):
    def assert_no_traceback(self, proc, tag: str) -> None:
        self.assertNotIn("Traceback", proc.stderr,
                         "%s leaked a traceback: %s" % (tag, proc.stderr[:400]))

    def test_hook_garbage_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".jev-tools.json").write_text(GARBAGE, encoding="utf-8")
            proc = run("inventory_hook.py", [], tmp,
                       env_extra={"JEV_HOOK_CWD": tmp,
                                  "JEV_HOOK_PROMPT": "deploy the proxy"},
                       stdin="{}")
            self.assert_no_traceback(proc, "sidecar")
            self.assertEqual(proc.returncode, 0)

    def test_hook_garbage_miss_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".jev-tools-miss.json").write_text(
                GARBAGE, encoding="utf-8")
            proc = run("inventory_hook.py", [], tmp,
                       env_extra={"JEV_HOOK_CWD": tmp,
                                  "JEV_HOOK_PROMPT": "deploy the proxy"},
                       stdin="{}")
            self.assert_no_traceback(proc, "miss sidecar")
            self.assertEqual(proc.returncode, 0)

    def test_decisions_garbage_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            log.write_text(GARBAGE + '{"ts":1,"q":"x"}\n', encoding="utf-8")
            for argv in (["--file", str(log), "--count"],
                         ["--file", str(log), "--json"]):
                proc = run("decisions.py", argv, tmp)
                self.assert_no_traceback(proc, "decisions %s" % argv[-1])

    def test_trace_garbage_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(GARBAGE, encoding="utf-8")
            for argv in (["--file", str(f), "show"],
                         ["--file", str(f), "stats"],
                         ["--file", str(f), "env"]):
                proc = run("trace.py", argv, tmp)
                self.assert_no_traceback(proc, "trace %s" % argv[-1])

    def test_compare_garbage_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "cases.json"
            f.write_text(GARBAGE, encoding="utf-8")
            proc = run("compare.py", ["--cases", str(f)], tmp)
            self.assert_no_traceback(proc, "compare cases")
            self.assertNotEqual(proc.returncode, 0)  # clean error, not crash

    def test_garbage_policy_everywhere(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "p.json"
            f.write_text(GARBAGE, encoding="utf-8")
            env = {"JEV_POLICY": str(f)}
            for script, argv in (
                ("inventory.py", ["--env"]),
                ("decisions.py", ["--env"]),
                ("compact.py", ["--env"]),
                ("policy_lint.py", []),
            ):
                proc = run(script, argv, tmp, env_extra=env)
                self.assert_no_traceback(proc, "policy %s" % script)


if __name__ == "__main__":
    unittest.main()
